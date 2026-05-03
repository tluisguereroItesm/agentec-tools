from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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

    return GraphSettings(
        capability=capability,
        profile_name=str(profile_name),
        tenant_id=str(tenant_id),
        client_id=str(client_id),
        authority_host=str(authority_host).rstrip("/"),
        scopes=scopes,
        token_store_dir=token_store_dir,
        allow_tenant_override=allow_override,
        default_drive_mode=str(profile.get("defaultDriveMode", "me")),
        site_hostname=str(profile.get("siteHostname", "")),
        site_path=str(profile.get("sitePath", "")),
    )


def auth_base_url(settings: GraphSettings) -> str:
    return f"{settings.authority_host}/{settings.tenant_id}/oauth2/v2.0"


def _session_dir(settings: GraphSettings) -> Path:
    session_dir = settings.token_store_dir / f"{_slug(settings.profile_name)}__{_slug(settings.tenant_id)[:16]}__{_slug(settings.client_id)[:16]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def token_path(settings: GraphSettings, user_id: str | None = None) -> Path:
    if user_id:
        return _session_dir(settings) / f"user-{_slug(user_id)}.json"
    return _session_dir(settings) / "owner.json"


def pending_path(settings: GraphSettings, user_id: str | None = None) -> Path:
    directory = _session_dir(settings) / "_pending"
    directory.mkdir(parents=True, exist_ok=True)
    if user_id:
        return directory / f"user-{_slug(user_id)}.json"
    return directory / "owner.json"


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
    file_path = token_path(settings, user_id)
    token_data["saved_at"] = int(time.time())
    token_data["profile_name"] = settings.profile_name
    token_data["tenant_id"] = settings.tenant_id
    token_data["client_id"] = settings.client_id
    if user_id:
        token_data["user_id"] = user_id
    file_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    file_path.chmod(0o600)


def load_token(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any] | None:
    file_path = token_path(settings, user_id)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def is_expired(token_data: dict[str, Any]) -> bool:
    saved_at = int(token_data.get("saved_at", 0))
    expires_in = int(token_data.get("expires_in", 3600))
    return (time.time() - saved_at) >= max(expires_in - 300, 0)


def init_login(settings: GraphSettings, user_id: str | None = None) -> dict[str, Any]:
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
        raise RuntimeError(f"AUTH_ERROR: no se pudo renovar token: {response.get('error_description', response)}")
    save_token(settings, response, user_id)
    return response


def get_valid_token(settings: GraphSettings, user_id: str | None = None) -> str:
    token_data = load_token(settings, user_id)
    if not token_data:
        raise RuntimeError(
            "AUTH_ERROR: no existe sesión Graph activa. Ejecuta auth.py login o auth.py init-login para el profile configurado."
        )
    if is_expired(token_data):
        token_data = refresh_token(settings, token_data, user_id)
    return str(token_data["access_token"])


def logout(settings: GraphSettings, user_id: str | None = None) -> None:
    token_path(settings, user_id).unlink(missing_ok=True)
    pending_path(settings, user_id).unlink(missing_ok=True)


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
        "GRAPH_ERROR",
        "RATE_LIMIT",
        "MISSING_ARG",
        "CONFIG_ERROR",
        "TENANT_OVERRIDE_DENIED",
    ]:
        if prefix in message:
            return prefix
    return "ERROR"


def graph_get_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    request.add_header("ConsistencyLevel", "eventual")
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
            raise RuntimeError("AUTH_ERROR: token inválido o expirado")
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
