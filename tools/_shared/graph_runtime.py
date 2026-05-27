from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ─── Mensajes orientados al agente MCP ────────────────────────────────────────
# Estos strings forman parte del contrato implícito con el LLM que consume las
# tools vía MCP. Si los modificas:
#   1. Mantén el prefijo (AUTH_ERROR:, GRAPH_ERROR:, etc.) porque
#      error_type_from_message() los usa para routing.
#   2. No menciones comandos shell ni nombres de scripts: el agente no tiene
#      shell ni Python disponibles, y si los menciona el mensaje se interpreta
#      como instrucción literal y el agente se desvía.
#   3. Si hay una acción remedial, descríbela como una llamada MCP
#      (action='auth-login', etc.).
#   4. Revisa que SKILL.md de las tools que usen este runtime sigan coincidiendo.

ERR_NO_SESSION = (
    "AUTH_ERROR: no existe sesión Graph activa. "
    "Para autenticar, llama esta misma tool con action='auth-login' "
    "y sigue las instrucciones que devuelva (URL + código). "
    "Cuando el usuario confirme, llama con action='auth-poll'."
)

ERR_TOKEN_REJECTED = (
    "AUTH_ERROR: token de Graph rechazado por el servidor "
    "(probablemente expirado). Llama esta tool con action='auth-login' "
    "para iniciar un nuevo login."
)

ERR_REFRESH_FAILED = (
    "AUTH_ERROR: no se pudo renovar el token de Graph: {detail}. "
    "Llama esta tool con action='auth-login' para iniciar un nuevo login."
)

# Cuando el MRRT exchange falla por consent o permisos faltantes en la app de
# Azure AD. El admin del tenant debe agregar los permisos del resource y hacer
# grant admin consent antes de que la tool pueda funcionar.
#
# IMPORTANTE: usamos prefijo CONSENT_ERROR (no AUTH_ERROR) para que el agente
# MCP sepa que NO debe reintentar auth-login. La sesión del usuario está bien;
# lo que falta es configuración de la app de Azure AD que solo un admin puede
# resolver. Si esto se reportara como AUTH_ERROR, el SKILL.md mandaría al
# agente a un nuevo device code login → otro MRRT fallido → loop infinito.
ERR_RESOURCE_NOT_CONSENTED = (
    "CONSENT_ERROR: el recurso '{resource}' no está habilitado en la app de Azure AD. "
    "Detalle del servidor: {detail}. "
    "El administrador del tenant debe agregar los permisos de este recurso "
    "en App registrations → API permissions y hacer grant admin consent. "
    "Un nuevo login del usuario NO resuelve esto — es configuración de la app."
)

# Zona horaria por defecto para operaciones de fecha/hora. Puede ser sobreescrita por env var.
DEFAULT_TIMEZONE = os.environ.get("AGENTEC_DEFAULT_TIMEZONE", "America/Mexico_City")


# ─── Mapeo capability → resource ─────────────────────────────────────────────
# Identifica a qué API server apunta cada capability. Las capabilities no
# listadas aquí asumen Graph (caso común: mail, files, calendar, teams, users,
# approvals, sharepoint-search).
#
# Los strings de resource son los audience URIs que Azure AD/Entra emite en
# el `aud` claim del JWT. Estos son fósiles arquitectónicos de Microsoft:
#   - "service.flow.microsoft.com" para Power Automate (no es el endpoint API,
#     que es api.flow.microsoft.com; aud y endpoint son entidades distintas).
#   - "analysis.windows.net/powerbi/api" para Power BI (herencia de cuando
#     Power BI compartía infra con Analysis Services).
# Estos URIs están documentados en learn.microsoft.com y son estables.

RESOURCE_GRAPH = "https://graph.microsoft.com"
RESOURCE_FLOW = "https://service.flow.microsoft.com"
RESOURCE_POWERBI = "https://analysis.windows.net/powerbi/api"

_CAPABILITY_RESOURCE_MAP: dict[str, str] = {
    "flows": RESOURCE_FLOW,
    "approvals": RESOURCE_FLOW,  # Power Automate Approvals API: api.flow.microsoft.com/.../approvals
    "powerbi": RESOURCE_POWERBI,
    # Resto de capabilities (mail, files, calendar, teams, users,
    # sharepoint-search) caen a RESOURCE_GRAPH por defecto.
}


def resolve_resource_for_capability(capability: str) -> str:
    """Devuelve el resource/audience URI al que apuntan los tokens de esta capability."""
    return _CAPABILITY_RESOURCE_MAP.get(capability, RESOURCE_GRAPH)


def _load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _discover_stack_config_dir() -> Path | None:
    explicit = os.environ.get("AGENTEC_STACK_CONFIG_DIR", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate

    env_file = os.environ.get("AGENTEC_STACK_ENV_FILE", "").strip()
    if env_file:
        candidate = Path(env_file).expanduser().parent
        if candidate.exists():
            return candidate

    cwd = Path.cwd().resolve()
    candidates: list[Path] = []
    for parent in (cwd, *cwd.parents):
        candidates.append(parent / "config")
        candidates.append(parent / "stack-config")

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_dir():
            return candidate

    return None


def _bootstrap_env() -> None:
    explicit = os.environ.get("AGENTEC_STACK_ENV_FILE", "").strip()
    if explicit:
        _load_env_file(Path(explicit).expanduser())
        return

    stack_cfg = _discover_stack_config_dir()
    if stack_cfg:
        _load_env_file(stack_cfg / "stack.env")


_bootstrap_env()


@dataclass
class GraphSettings:
    capability: str
    profile_name: str
    tenant_id: str
    client_id: str
    authority_host: str
    scopes: str
    token_store_dir: Path
    allow_tenant_override: bool
    # Resource/audience al que apuntan los tokens de esta capability.
    # Se resuelve automáticamente desde capability vía
    # resolve_resource_for_capability(). Default es Graph para mantener
    # backward-compat con capabilities preexistentes (mail, files, etc.).
    resource: str = RESOURCE_GRAPH
    default_drive_mode: str = "me"
    site_hostname: str = ""
    site_path: str = ""


DEFAULT_MAIL_SCOPES = "User.Read Mail.Read Mail.ReadBasic offline_access"
DEFAULT_FILES_SCOPES = "User.Read Files.Read Files.Read.All Sites.Read.All offline_access"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_scopes(value: Any, fallback: str) -> str:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return " ".join(parts) if parts else fallback
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _slug(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "default")
    return safe.strip("-") or "default"


def resolve_stack_config_dir() -> Path | None:
    return _discover_stack_config_dir()


def resolve_timezone(raw: dict) -> str:
    """Devuelve la zona horaria para esta invocación.
    Prioridad: 'timezone' del payload del agente > default global."""
    tz = raw.get("timezone")
    return str(tz) if tz else DEFAULT_TIMEZONE


def load_profile_document(kind: str, explicit_file: str | None = None) -> dict[str, Any]:
    """Load profile JSON. explicit_file is ONLY accepted from env, never from user input."""
    candidates: list[Path] = []
    # NOTE: explicit_file parameter is intentionally NOT used here to prevent path traversal.
    # Config paths are always resolved from trusted env vars or the stack config dir.

    env_specific = os.environ.get(
        "AGENTEC_WEB_LOGIN_CONFIG_FILE" if kind == "web-login" else "AGENTEC_GRAPH_CONFIG_FILE",
        "",
    ).strip()
    if env_specific:
        # Resolve to real path and verify it stays within the expected config dir
        resolved = Path(env_specific).expanduser().resolve()
        stack_dir = resolve_stack_config_dir()
        allowed_prefix = (stack_dir.resolve() if stack_dir else resolved.parent)
        if resolved == allowed_prefix or allowed_prefix in resolved.parents:
            candidates.append(resolved)

    stack_dir = resolve_stack_config_dir()
    if stack_dir:
        candidates.append((stack_dir / "tools" / kind / "profiles.json").resolve())
        candidates.append((stack_dir / "tools" / kind / "profiles.example.json").resolve())

    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {"profiles": {}}


def resolve_graph_settings(capability: str, input_data: dict[str, Any]) -> GraphSettings:
    # configFile from user input is silently ignored — config path is env-only (path traversal prevention)
    document = load_profile_document("graph")
    profile_name = (
        input_data.get("profile")
        or os.environ.get("AGENTEC_GRAPH_PROFILE")
        or document.get("defaultProfile")
        or "default"
    )
    profile = (document.get("profiles") or {}).get(profile_name, {})
    allow_override = _bool_env("AGENTEC_GRAPH_ALLOW_TENANT_OVERRIDE", True)

    tenant_override = input_data.get("tenantIdOverride")
    client_override = input_data.get("clientIdOverride")
    if (tenant_override or client_override) and not allow_override:
        raise RuntimeError("TENANT_OVERRIDE_DENIED: los overrides de tenant/client no están permitidos")

    tenant_id = (
        tenant_override
        or profile.get("tenantId")
        or os.environ.get("AGENTEC_GRAPH_DEFAULT_TENANT_ID", "").strip()
    )
    client_id = (
        client_override
        or profile.get("clientId")
        or os.environ.get("AGENTEC_GRAPH_DEFAULT_CLIENT_ID", "").strip()
    )
    authority_host = (
        profile.get("authorityHost")
        or os.environ.get("AGENTEC_GRAPH_AUTHORITY_HOST", "https://login.microsoftonline.com").strip()
    )

    if not tenant_id:
        raise RuntimeError("CONFIG_ERROR: falta tenantId en profile, env o override")
    if not client_id:
        raise RuntimeError("CONFIG_ERROR: falta clientId en profile, env o override")

    # NOTA importante sobre scopes vs resource:
    #
    # Los `scopes` aquí siempre son los del PRIMER login (device code), que
    # va contra Graph y obtiene el refresh_token. Aunque la capability sea
    # "flows" o "powerbi", el device code se hace con scopes Graph (los
    # combinedScopes incluyen offline_access, que es lo crítico).
    #
    # Para los recursos secundarios (Flow, Power BI), el access_token se
    # obtiene vía MRRT en get_valid_token_for_resource(), usando el
    # refresh_token de Graph. Eso se hace bajo demanda, no aquí.
    #
    # Conclusión: NO hace falta poner scopes de Flow/PowerBI en
    # combinedScopes ni en flowsScopes/powerbiScopes. Los scopes del MRRT
    # exchange son siempre `https://{resource}/.default` (toma lo que esté
    # consented en la app), salvo que el caller quiera granularidad fina,
    # en cuyo caso puede sobreescribir vía resolve_scopes_for_resource().
    if profile.get("combinedScopes"):
        scopes = _normalize_scopes(profile["combinedScopes"], DEFAULT_MAIL_SCOPES)
    else:
        # Generic scope resolution: "mail" → mailScopes, "powerbi" → powerbiScopes, etc.
        _SCOPE_MAP: dict[str, tuple[str, str, str]] = {
            "mail": ("mailScopes", "AGENTEC_GRAPH_MAIL_SCOPES", DEFAULT_MAIL_SCOPES),
            "files": ("filesScopes", "AGENTEC_GRAPH_FILES_SCOPES", DEFAULT_FILES_SCOPES),
        }
        _prof_key, _env_key, _default = _SCOPE_MAP.get(
            capability,
            (f"{capability}Scopes", f"AGENTEC_GRAPH_{capability.upper()}_SCOPES", DEFAULT_FILES_SCOPES),
        )
        env_scopes = os.environ.get(_env_key, _default)
        scopes = _normalize_scopes(profile.get(_prof_key), env_scopes)

    token_store_dir = Path(
        os.environ.get("AGENTEC_GRAPH_TOKEN_STORE_DIR", str(Path.home() / ".agentec-graph-tokens"))
    ).expanduser()
    token_store_dir.mkdir(parents=True, exist_ok=True)

    # Resource derivado de capability. mail/files/calendar/teams/etc → Graph.
    # flows → Flow Service. powerbi → Power BI Service.
    resource = resolve_resource_for_capability(capability)

    return GraphSettings(
        capability=capability,
        profile_name=str(profile_name),
        tenant_id=str(tenant_id),
        client_id=str(client_id),
        authority_host=str(authority_host).rstrip("/"),
        scopes=scopes,
        token_store_dir=token_store_dir,
        allow_tenant_override=allow_override,
        resource=resource,
        default_drive_mode=str(profile.get("defaultDriveMode", "me")),
        site_hostname=str(profile.get("siteHostname", "")),
        site_path=str(profile.get("sitePath", "")),
    )


def resolve_scopes_for_resource(settings: GraphSettings, resource: str) -> str:
    """Scopes a usar en un MRRT exchange para `resource`.

    Para Graph (resource principal), usa los scopes ya configurados en
    settings.scopes (combinedScopes del profile).

    Para recursos secundarios (Flow, Power BI), usa `{resource}/.default`,
    que pide todos los scopes que estén consented en la app de Azure AD
    para ese resource. Esto es lo más simple y robusto — no requiere
    saber qué scope granular tiene cada tool, solo que el admin haya
    consentido los permisos.

    Si en el futuro alguna tool necesita granularidad (ej. Flow con
    Flows.Read.All específicamente y no user_impersonation), se puede
    leer un override del profile aquí.
    """
    if resource == RESOURCE_GRAPH:
        return settings.scopes
    return f"{resource}/.default"


def auth_base_url(settings: GraphSettings) -> str:
    return f"{settings.authority_host}/{settings.tenant_id}/oauth2/v2.0"


def _session_dir(settings: GraphSettings) -> Path:
    session_dir = settings.token_store_dir / f"{_slug(settings.profile_name)}__{_slug(settings.tenant_id)[:16]}__{_slug(settings.client_id)[:16]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def token_path(settings: GraphSettings, user_id: str | None = None) -> Path:
    """Path del cache principal (Graph). Este archivo contiene el refresh_token
    compartido que sirve para MRRT contra todos los resources secundarios."""
    if user_id:
        return _session_dir(settings) / f"user-{_slug(user_id)}.json"
    return _session_dir(settings) / "owner.json"


def _resource_slug(resource: str) -> str:
    """Slug corto del resource para usar en nombres de archivo de cache.
    Ej: 'https://service.flow.microsoft.com' → 'service-flow-microsoft-com'."""
    parsed = urllib.parse.urlparse(resource)
    netloc = parsed.netloc or resource
    # Concatena netloc + path para diferenciar (analysis.windows.net/powerbi/api
    # vs analysis.windows.net/otra-cosa, hipotéticamente).
    raw = f"{netloc}{parsed.path}".rstrip("/")
    return _slug(raw)[:48]


def token_path_for_resource(
    settings: GraphSettings,
    user_id: str | None,
    resource: str,
) -> Path:
    """Path del cache de un resource secundario (Flow, Power BI).

    Para resource == Graph delega a token_path() (cache principal compartido).
    Para otros resources, sufija el nombre con un slug del resource:
        owner.service-flow-microsoft-com.json
        owner.analysis-windows-net-powerbi-api.json

    Esto mantiene el `owner.json` principal como el "anchor" que tiene el
    refresh_token de Graph, mientras los caches secundarios solo guardan
    access_tokens que se pueden tirar y regenerar sin perder sesión.
    """
    if resource == RESOURCE_GRAPH:
        return token_path(settings, user_id)
    base = "owner" if not user_id else f"user-{_slug(user_id)}"
    return _session_dir(settings) / f"{base}.{_resource_slug(resource)}.json"


def pending_path(settings: GraphSettings, user_id: str | None = None) -> Path:
    directory = _session_dir(settings) / "_pending"
    directory.mkdir(parents=True, exist_ok=True)
    if user_id:
        return directory / f"user-{_slug(user_id)}.json"
    return directory / "owner.json"


def resolve_session_user(settings: GraphSettings, user_id: str | None) -> str | None:
    """
    Normaliza el user_id para operaciones de sesión.

    Las tools de Graph aceptan un parámetro `user` opcional que sirve para
    diferenciar múltiples sesiones bajo el mismo profile+tenant+client. Cuando
    `user` es None, la sesión se almacena en `owner.json`; cuando es un string,
    en `user-<slug>.json`.

    Caso single-user (típico hoy):
        Si el agente pasa un `user_id` pero solo existe la sesión `owner.json`
        del profile (no hay `user-<x>.json` correspondiente), se trata como
        None para reutilizar esa sesión. Esto evita que el agente fragmente
        sesiones por ser inconsistente entre auth-login (sin user) y las
        operaciones subsecuentes (con user, p.ej. el email del agente).

    Limitación:
        En un escenario multi-usuario real (varios humanos compartiendo un
        profile), esta heurística puede colisionar: el primer usuario que
        haga login sin `user` consume el slot `owner`, y todos los demás
        verán su `user-<x>` redirigido a esa sesión. Cuando ese caso llegue,
        hay que sustituir esta función por un contrato explícito de sesión
        (p.ej. el agente recibe un sessionKey en auth-poll y lo pasa tal cual
        en llamadas posteriores).

    Args:
        settings: configuración Graph resuelta.
        user_id: valor que el agente pasó como `user` en el input (o None).

    Returns:
        El user_id efectivo a usar para `token_path` / `load_token` / etc.
        None si conviene resolver al slot `owner`.
    """
    if not user_id or not str(user_id).strip():
        return None
    user_str = str(user_id).strip()
    if not token_path(settings, user_str).exists() and token_path(settings, None).exists():
        # Telemetría: log a stderr para que aparezca en docker logs sin mezclarse
        # con el stdout JSON que la tool emite como resultado.
        print(
            f"[graph_runtime] resolve_session_user: redirigiendo user={user_str!r} "
            f"a slot owner (profile={settings.profile_name})",
            file=sys.stderr,
        )
        return None
    return user_str


def http_post(url: str, data: dict[str, Any]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"error": str(exc)}


def save_token(settings: GraphSettings, token_data: dict[str, Any], user_id: str | None = None) -> None:
    """Guarda el token del cache principal (Graph). Este archivo es el "anchor"
    de la sesión: contiene el refresh_token que MRRT reutiliza para otros resources."""
    file_path = token_path(settings, user_id)
    token_data["saved_at"] = int(time.time())
    token_data["profile_name"] = settings.profile_name
    token_data["tenant_id"] = settings.tenant_id
    token_data["client_id"] = settings.client_id
    token_data["resource"] = RESOURCE_GRAPH
    if user_id:
        token_data["user_id"] = user_id
    file_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    file_path.chmod(0o600)


def save_token_for_resource(
    settings: GraphSettings,
    token_data: dict[str, Any],
    resource: str,
    user_id: str | None = None,
) -> None:
    """Guarda un access_token secundario (Flow, Power BI) en su propio archivo.

    No incluye refresh_token aquí (el refresh_token del response del MRRT
    exchange es el mismo del owner.json — Entra rota el refresh_token y la
    propagación al cache principal se hace en refresh_token_for_resource).
    """
    if resource == RESOURCE_GRAPH:
        save_token(settings, token_data, user_id)
        return
    file_path = token_path_for_resource(settings, user_id, resource)
    token_data["saved_at"] = int(time.time())
    token_data["profile_name"] = settings.profile_name
    token_data["tenant_id"] = settings.tenant_id
    token_data["client_id"] = settings.client_id
    token_data["resource"] = resource
    if user_id:
        token_data["user_id"] = user_id
    file_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    file_path.chmod(0o600)


def load_token(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any] | None:
    file_path = token_path(settings, user_id)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def load_token_for_resource(
    settings: GraphSettings,
    resource: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Carga el cache secundario de un resource específico (o el principal si resource==Graph)."""
    if resource == RESOURCE_GRAPH:
        return load_token(settings, user_id)
    file_path = token_path_for_resource(settings, user_id, resource)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def is_expired(token_data: dict[str, Any]) -> bool:
    saved_at = int(token_data.get("saved_at", 0))
    expires_in = int(token_data.get("expires_in", 3600))
    return (time.time() - saved_at) >= max(expires_in - 300, 0)


def init_login(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any]:
    """Inicia el device code flow.

    Nota: el device code SIEMPRE pide scopes de Graph (los del profile o
    combinedScopes), aunque la capability sea flows o powerbi. La razón es
    que Azure AD/Entra v2.0 endpoint NO permite scopes de múltiples
    resources en una sola request (devuelve AADSTS28000). Por eso el
    diseño es: device code → Graph + offline_access, luego MRRT exchange
    contra Flow/PowerBI usando el refresh_token resultante.

    Para que el MRRT exchange funcione sin pedirle consent extra al
    usuario, los permisos del resource secundario deben estar pre-consented
    en la app de Azure AD (admin consent del tenant).
    """
    response = http_post(
        f"{auth_base_url(settings)}/devicecode",
        {"client_id": settings.client_id, "scope": settings.scopes},
    )
    if "error" in response:
        return {"status": "error", "error": response.get("error_description", str(response))}

    pending = {
        "device_code": response["device_code"],
        "user_code": response["user_code"],
        "verification_uri": response["verification_uri"],
        "expires_in": response.get("expires_in", 900),
        "interval": response.get("interval", 5),
        "created_at": int(time.time()),
    }
    file_path = pending_path(settings, user_id)
    file_path.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    file_path.chmod(0o600)
    return {
        "status": "pending",
        "user_code": response["user_code"],
        "verification_uri": response["verification_uri"],
        "expires_in": response.get("expires_in", 900),
        "profile": settings.profile_name,
        "tenantId": settings.tenant_id,
        "message": f"Abre {response['verification_uri']} e ingresa el código {response['user_code']}",
    }


def poll_login(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any]:
    file_path = pending_path(settings, user_id)
    if not file_path.exists():
        return {"status": "error", "error": "No hay login pendiente. Ejecuta init-login primero."}

    pending = json.loads(file_path.read_text(encoding="utf-8"))
    created_at = int(pending.get("created_at", 0))
    expires_in = int(pending.get("expires_in", 900))
    if (time.time() - created_at) > expires_in:
        file_path.unlink(missing_ok=True)
        return {"status": "expired", "error": "El código expiró. Ejecuta init-login nuevamente."}

    token_data = http_post(
        f"{auth_base_url(settings)}/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": settings.client_id,
            "device_code": pending["device_code"],
        },
    )

    if "access_token" in token_data:
        save_token(settings, token_data, user_id)
        file_path.unlink(missing_ok=True)
        return {
            "status": "ok",
            "profile": settings.profile_name,
            "tenantId": settings.tenant_id,
            "tokenPath": str(token_path(settings, user_id)),
        }

    error = token_data.get("error", "")
    if error == "authorization_pending":
        return {
            "status": "pending",
            "user_code": pending.get("user_code", ""),
            "remaining_s": expires_in - int(time.time() - created_at),
        }
    if error == "expired_token":
        file_path.unlink(missing_ok=True)
        return {"status": "expired", "error": "El código expiró."}

    file_path.unlink(missing_ok=True)
    return {"status": "error", "error": token_data.get("error_description", str(token_data))}


def device_code_login(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any]:
    response = init_login(settings, user_id)
    if response.get("status") != "pending":
        raise RuntimeError(response.get("error", "No se pudo iniciar login"))

    print("\n🔐 Autorización requerida")
    print(f"Perfil: {settings.profile_name}")
    print(f"Tenant: {settings.tenant_id}")
    print(f"Abre: {response['verification_uri']}")
    print(f"Código: {response['user_code']}")
    print("Esperando autorización", end="", flush=True)

    deadline = time.time() + int(response.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(5)
        print(".", end="", flush=True)
        polled = poll_login(settings, user_id)
        if polled.get("status") == "ok":
            print("\n✅ Autorización exitosa")
            return polled
        if polled.get("status") == "expired":
            raise RuntimeError("AUTH_ERROR: El código expiró durante login")
        if polled.get("status") == "error":
            raise RuntimeError(f"AUTH_ERROR: {polled.get('error')}")

    raise RuntimeError("AUTH_ERROR: Tiempo de espera agotado durante login")


def refresh_token(settings: GraphSettings, token_data: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    """Renueva el access_token de Graph usando el refresh_token cacheado.

    Para renovar tokens de resources secundarios (Flow, Power BI) usa
    refresh_token_for_resource() en su lugar.
    """
    response = http_post(
        f"{auth_base_url(settings)}/token",
        {
            "grant_type": "refresh_token",
            "client_id": settings.client_id,
            "refresh_token": token_data.get("refresh_token", ""),
            "scope": settings.scopes,
        },
    )
    if "access_token" not in response:
        raise RuntimeError(ERR_REFRESH_FAILED.format(detail=response.get("error_description", response)))
    save_token(settings, response, user_id)
    return response


def refresh_token_for_resource(
    settings: GraphSettings,
    resource: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """MRRT exchange: usa el refresh_token del cache principal (Graph) para
    obtener un access_token nuevo apuntando a `resource`.

    Esta es la pieza clave del soporte multi-audience. El refresh_token de
    Entra ID es agnóstico de audience; solo el access_token lleva el `aud`.
    Cambiando el scope en el grant `refresh_token` se obtiene un access_token
    para el resource pedido — siempre y cuando los permisos estén consented
    en la app de Azure AD.

    Errores que esperamos manejar:
    - AADSTS65001: el usuario no consintió los permisos del resource.
    - AADSTS50105: la app no tiene los permisos asignados.
    - AADSTS70011: scope inválido (el resource no existe o está mal escrito).
    - invalid_grant: refresh_token expirado o revocado.

    Cuando alguno de los anteriores ocurre, devolvemos ERR_RESOURCE_NOT_CONSENTED
    para que el agente sepa que el problema es del lado de Azure AD, no del
    runtime. El usuario no necesita un nuevo login — el admin necesita hacer
    grant admin consent en el portal.
    """
    if resource == RESOURCE_GRAPH:
        # Para Graph, la función dedicada es refresh_token() (sin _for_resource).
        # Esto no debería pasar en flujo normal, pero lo soportamos.
        primary = load_token(settings, user_id)
        if not primary:
            raise RuntimeError(ERR_NO_SESSION)
        return refresh_token(settings, primary, user_id)

    primary = load_token(settings, user_id)
    if not primary or not primary.get("refresh_token"):
        raise RuntimeError(ERR_NO_SESSION)

    scopes = resolve_scopes_for_resource(settings, resource)
    response = http_post(
        f"{auth_base_url(settings)}/token",
        {
            "grant_type": "refresh_token",
            "client_id": settings.client_id,
            "refresh_token": primary["refresh_token"],
            "scope": scopes,
        },
    )

    if "access_token" not in response:
        error_code = response.get("error", "")
        detail = response.get("error_description", json.dumps(response, ensure_ascii=False))
        consent_markers = [
            "AADSTS65001",  # User/admin not consented
            "AADSTS50105",  # App has no role/permission assigned
            "AADSTS70011",  # Invalid scope
            "AADSTS65002",  # Consent between admin and user mismatched
            "consent_required",
            "interaction_required",
            "invalid_scope",
        ]
        if any(marker in detail for marker in consent_markers):
            raise RuntimeError(
                ERR_RESOURCE_NOT_CONSENTED.format(resource=resource, detail=detail)
            )
        # Otros errores (invalid_grant, etc.): el refresh_token mismo está mal.
        # El usuario sí necesita re-login en este caso.
        raise RuntimeError(ERR_REFRESH_FAILED.format(detail=detail))

    # Entra rota los refresh_tokens: cada exchange devuelve uno nuevo. Si lo
    # ignoramos, eventualmente el viejo expira y todo se cae. Lo propagamos
    # al cache principal para que la próxima vez se use el más reciente.
    new_refresh = response.get("refresh_token")
    if new_refresh and new_refresh != primary.get("refresh_token"):
        primary["refresh_token"] = new_refresh
        # Mantener saved_at del primary intacto: el access_token de Graph que
        # tenga ahí no cambia, solo el refresh_token sí. Actualizamos en disco.
        token_path(settings, user_id).write_text(
            json.dumps(primary, indent=2), encoding="utf-8"
        )

    save_token_for_resource(settings, response, resource, user_id)
    return response


def get_valid_token(settings: GraphSettings, user_id: str | None = None) -> str:
    """Devuelve un access_token válido para el resource asociado a la capability.

    Esta es la API pública que las tools llaman. El resource se resuelve
    automáticamente desde settings.resource (que a su vez se deriva de
    capability vía resolve_resource_for_capability):

      capability="mail"    → resource=Graph        → token Graph
      capability="files"   → resource=Graph        → token Graph
      capability="flows"   → resource=Flow Service → token Flow (vía MRRT)
      capability="powerbi" → resource=Power BI     → token Power BI (vía MRRT)

    Las tools no necesitan saber cuál es su resource — solo llaman
    get_valid_token(settings, user_id) y reciben el token correcto.

    Para casos avanzados donde una sola tool necesite tokens de múltiples
    resources (raro pero posible), usar get_valid_token_for_resource()
    directamente.
    """
    return get_valid_token_for_resource(settings, settings.resource, user_id)


def get_valid_token_for_resource(
    settings: GraphSettings,
    resource: str,
    user_id: str | None = None,
) -> str:
    """Devuelve un access_token válido para `resource` específicamente.

    Lógica:
    1. Para Graph: respeta SSO injected y app_secret (escenarios especiales),
       luego cache principal con refresh si está expirado.
    2. Para resources secundarios: cache propio si está vigente; si no,
       MRRT exchange usando el refresh_token del cache principal.
    """
    if resource == RESOURCE_GRAPH:
        # SSO token injected via env var (e.g. from Teams OAuth flow) takes priority.
        # Aplica solo a Graph — los tokens SSO de Teams no son válidos para Flow/PowerBI.
        sso_token = os.environ.get("AGENTEC_GRAPH_SSO_TOKEN", "").strip()
        if sso_token:
            return sso_token

        # App-only token via client_credentials (Application permissions + admin consent).
        # Similar: aplica a Graph en este runtime. Para Flow/PowerBI con service
        # principal, habría que hacer una función dedicada con sus particularidades
        # (Power BI requiere habilitar service principals en el admin portal del producto).
        app_secret = os.environ.get("AGENTEC_GRAPH_APP_SECRET", "").strip()
        if app_secret:
            return get_app_token(settings, app_secret)

        token_data = load_token(settings, user_id)
        if not token_data:
            raise RuntimeError(ERR_NO_SESSION)
        if is_expired(token_data):
            token_data = refresh_token(settings, token_data, user_id)
        return str(token_data["access_token"])

    # Resource secundario (Flow, Power BI, etc.)
    cached = load_token_for_resource(settings, resource, user_id)
    if cached and not is_expired(cached):
        return str(cached["access_token"])

    # Cache vacío o expirado → MRRT exchange.
    response = refresh_token_for_resource(settings, resource, user_id)
    return str(response["access_token"])


def logout(settings: GraphSettings, user_id: str | None = None) -> None:
    """Cierra sesión: borra el cache principal Y todos los caches secundarios
    de resources (Flow, Power BI). El refresh_token muere con el primary."""
    token_path(settings, user_id).unlink(missing_ok=True)
    pending_path(settings, user_id).unlink(missing_ok=True)
    # Borrar caches secundarios. El pattern es owner.{slug}.json o
    # user-{user}.{slug}.json. Borramos todo lo que coincida con el prefijo.
    session_dir = _session_dir(settings)
    base = "owner" if not user_id else f"user-{_slug(user_id)}"
    for secondary in session_dir.glob(f"{base}.*.json"):
        secondary.unlink(missing_ok=True)


def list_tokens(settings: GraphSettings) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    session_dir = _session_dir(settings)
    for candidate in sorted(session_dir.glob("*.json")):
        token_data = json.loads(candidate.read_text(encoding="utf-8"))
        records.append(
            {
                "file": str(candidate),
                "expired": is_expired(token_data),
                "hasRefresh": bool(token_data.get("refresh_token")),
                "profile": token_data.get("profile_name", settings.profile_name),
                "tenantId": token_data.get("tenant_id", settings.tenant_id),
                "resource": token_data.get("resource", RESOURCE_GRAPH),
            }
        )
    return records


def ensure_artifacts_dir() -> Path:
    artifacts = Path.cwd() / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


def write_result_artifact(tool_name: str, action: str, payload: dict[str, Any]) -> str:
    artifacts = ensure_artifacts_dir()
    artifact_path = artifacts / f"{tool_name}-{action}-{int(time.time())}.json"
    artifact_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(artifact_path)


def build_success_result(message: str, data: dict[str, Any], settings: GraphSettings) -> dict[str, Any]:
    return {
        "success": True,
        "message": message,
        "data": data,
        "profile": settings.profile_name,
        "tenantId": settings.tenant_id,
        "backend": "python-urllib",
        "timestamp": datetime.now(UTC).isoformat(),
    }


def build_error_result(message: str, error_type: str, settings: GraphSettings | None = None) -> dict[str, Any]:
    payload = {
        "success": False,
        "message": message,
        "errorType": error_type,
        "backend": "python-urllib",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if settings:
        payload["profile"] = settings.profile_name
        payload["tenantId"] = settings.tenant_id
    return payload


def error_type_from_message(message: str) -> str:
    for prefix in [
        "AUTH_ERROR",
        "CONSENT_ERROR",
        "GRAPH_ERROR",
        "FLOW_ERROR",
        # graph-powerbi emite "PBI_ERROR"; graph-approvals emite
        # "APPROVALS_ERROR". Deben coincidir literalmente con el prefijo del
        # mensaje que lanza cada tool, no con el nombre del producto.
        "PBI_ERROR",
        "APPROVALS_ERROR",
        "RATE_LIMIT",
        "MISSING_ARG",
        "CONFIG_ERROR",
        "TENANT_OVERRIDE_DENIED",
    ]:
        if prefix in message:
            return prefix
    return "ERROR"


def graph_get_json(url: str, token: str, *, timezone: str | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    request.add_header("ConsistencyLevel", "eventual")
    if timezone:
        request.add_header("Prefer", f'outlook.timezone="{timezone}"')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        code = exc.code
        message = body.get("error", {}).get("message", str(exc))
        if code == 401:
            raise RuntimeError(ERR_TOKEN_REJECTED)
        if code == 403:
            raise RuntimeError(f"GRAPH_ERROR: [403] {message}")
        if code == 404:
            raise RuntimeError(f"GRAPH_ERROR: [404] {message}")
        if code == 429:
            raise RuntimeError("RATE_LIMIT: demasiadas solicitudes a Graph")
        raise RuntimeError(f"GRAPH_ERROR: [{code}] {message}")


def graph_download(url: str, token: str, destination: Path) -> None:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())


def run_auth_cli(capability: str) -> None:
    parser = argparse.ArgumentParser(description=f"Auth CLI para graph-{capability}")
    parser.add_argument("command", choices=["login", "init-login", "poll-login", "logout", "status", "refresh", "list"])
    parser.add_argument("--profile", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = resolve_graph_settings(
        capability,
        {
            "profile": args.profile,
            "tenantIdOverride": args.tenant_id,
            "clientIdOverride": args.client_id,
        },
    )

    if args.command == "login":
        result = device_code_login(settings, args.user)
    elif args.command == "init-login":
        result = init_login(settings, args.user)
    elif args.command == "poll-login":
        result = poll_login(settings, args.user)
    elif args.command == "logout":
        logout(settings, args.user)
        result = {"status": "ok", "message": "Sesión cerrada", "profile": settings.profile_name}
    elif args.command == "status":
        token_data = load_token(settings, args.user)
        result = {
            "status": "active" if token_data and not is_expired(token_data) else "expired" if token_data else "no_session",
            "profile": settings.profile_name,
            "tenantId": settings.tenant_id,
            "tokenPath": str(token_path(settings, args.user)),
            "resource": settings.resource,
        }
    elif args.command == "refresh":
        token_data = load_token(settings, args.user)
        if not token_data:
            raise RuntimeError("AUTH_ERROR: no existe token para refrescar")
        refresh_token(settings, token_data, args.user)
        result = {"status": "ok", "message": "Token renovado", "profile": settings.profile_name}
    else:
        result = {"status": "ok", "items": list_tokens(settings), "profile": settings.profile_name}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))