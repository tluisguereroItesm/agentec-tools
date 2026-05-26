from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

def _bootstrap_shared_path() -> None:
    candidates: list[Path] = []

    explicit = os.environ.get("AGENTEC_SHARED_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())

    tools_dir = os.environ.get("AGENTEC_TOOLS_DIR", "").strip()
    if tools_dir:
        candidates.append(Path(tools_dir).expanduser() / "_shared")

    here = Path(__file__).resolve()
    for base in (here, Path.cwd().resolve()):
        for parent in (base, *base.parents):
            candidates.append(parent / "_shared")
            candidates.append(parent / "tools" / "_shared")

    candidates.extend([
        Path("/app/external-tools/_shared"),
        Path("/app/_shared"),
        Path("/_shared"),
    ])

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_dir():
            if key not in sys.path:
                sys.path.insert(0, key)
            return


_bootstrap_shared_path()
from graph_runtime import (
    build_error_result,
    build_success_result,
    error_type_from_message,
    get_valid_token,
    init_login,
    poll_login,
    resolve_graph_settings,
    resolve_session_user,
    write_result_artifact,
)

# Power Automate Approvals API.
#
# La doc oficial de Microsoft NO documenta estos endpoints — son los que usa
# la UI web de Power Automate y la app Approvals de Teams internamente. Los
# tomamos de comunidad (elnathsoft, tomriha) y de inspección de tráfico de la
# app oficial. La API es estable desde 2016 (api-version=2016-11-01).
#
# Importante: a diferencia de /flows que acepta "~default" como environment,
# /approvalViews requiere el ID explícito. Lo resolvemos vía
# /environments?$filter=properties/isDefault+eq+true al primer uso.
FLOW_BASE = "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"
API_VERSION = "2016-11-01"

ACTION_ALIASES: dict[str, str] = {
    "list": "pending",
    "pendientes": "pending",
    "pending": "pending",
    "todas": "all",
    "all": "all",
    "historial": "history",
    "history": "history",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _flow_get(token: str, path: str) -> dict:
    """GET contra api.flow.microsoft.com. La api-version se agrega aquí
    automáticamente si no viene en `path`."""
    sep = "&" if "?" in path else "?"
    url = f"{FLOW_BASE}{path}{sep}api-version={API_VERSION}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
            msg = body.get("error", {}).get("message", str(exc))
        except Exception:
            msg = str(exc)
        if exc.code == 401:
            from graph_runtime import ERR_TOKEN_REJECTED
            raise RuntimeError(ERR_TOKEN_REJECTED) from exc
        raise RuntimeError(f"APPROVALS_ERROR: [{exc.code}] {msg}") from exc


def _resolve_default_environment(token: str, override: str) -> str:
    """Devuelve el environment ID a usar para approvals.

    Si `override` viene del input, se respeta tal cual. Si no, consulta
    /environments y devuelve el que tenga properties.isDefault=true.
    Como último recurso, deriva 'Default-{tenantId}' del JWT del token,
    que es el formato canónico del default environment en la mayoría de
    los tenants.
    """
    if override and override.strip():
        return override.strip()

    # Intento 1: preguntarle a la API cuál es el default
    try:
        data = _flow_get(token, "/environments?$top=50")
        for env in data.get("value", []):
            props = env.get("properties", {}) or {}
            if props.get("isDefault") is True:
                env_id = env.get("name") or env.get("id", "")
                if env_id:
                    return env_id
        # Si no hay isDefault explícito, toma el primero (común en tenants
        # con un solo environment).
        if data.get("value"):
            first = data["value"][0]
            env_id = first.get("name") or first.get("id", "")
            if env_id:
                return env_id
    except Exception:
        pass

    # Intento 2: derivar Default-{tenantId} del JWT. Sin librería de JWT
    # decoding, hacemos parsing manual del segundo segmento (payload).
    try:
        import base64
        payload_b64 = token.split(".")[1]
        # Padding necesario para base64 estándar
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        tid = payload.get("tid") or payload.get("tenantId")
        if tid:
            return f"Default-{tid}"
    except Exception:
        pass

    raise RuntimeError(
        "MISSING_ARG: no se pudo resolver el environment para approvals. "
        "Pasa el parámetro 'environment' con el ID exacto."
    )


def _relative(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        hours = int(delta.total_seconds() / 3600)
        if hours < 1:
            return "hace menos de 1h"
        if hours < 24:
            return f"hace {hours}h"
        days = hours // 24
        if days == 1:
            return "ayer"
        return f"hace {days} días"
    except Exception:
        return date_str


def _hours_pending(date_str: str) -> int:
    if not date_str:
        return 0
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 0


def _fmt_approval(item: dict) -> dict:
    """Normaliza un approval de /approvalViews a la forma que espera el SKILL.

    La estructura real de la API:
        {
          "id": "/providers/.../approvals/<approvalId>",
          "name": "<approvalId>",              # ← el GUID limpio
          "type": "...",
          "properties": {
            "title": "...",
            "details": "...",                  # texto descripcional largo
            "itemLinks": [...],
            "responseOptions": ["Approve", "Reject"],
            "creationDate": "2026-...",
            "isActive": true,
            "userRole": "Approver",
            "requestor": "user@tenant.onmicrosoft.com",
            "owner": { "displayName": "..." }
          }
        }

    Mantenemos el shape que usa el SKILL.md original (id, title, requestor,
    status, created, etc.) para no romper los flujos de conversación.
    """
    props = item.get("properties", {}) or {}
    requestor_obj = props.get("owner") or {}
    requestor_name = (
        requestor_obj.get("displayName")
        or props.get("requestor", "")
        or "(desconocido)"
    )
    created = props.get("creationDate") or props.get("createdDate") or ""
    hours = _hours_pending(created)
    return {
        "id": item.get("name") or item.get("id", ""),
        "title": props.get("title", ""),
        "requestor": requestor_name,
        "status": "Pending" if props.get("isActive") else "Completed",
        "userRole": props.get("userRole", ""),
        "created": _relative(created),
        "hoursPending": hours,
        "isUrgent": hours >= 24 and props.get("isActive", False),
        "details": (props.get("details") or "")[:500],
        "responseOptions": props.get("responseOptions") or [],
    }


def _list_approvals(token: str, environment: str, filter_clause: str, top: int) -> list[dict]:
    path = f"/environments/{urllib.parse.quote(environment)}/approvalViews?$top={top}"
    if filter_clause:
        # Preservar +, ', / sin escapar; el resto sí se url-encodea.
        safe_chars = "+'/"
        encoded_filter = urllib.parse.quote(filter_clause, safe=safe_chars)
        path += f"&$filter={encoded_filter}"
    data = _flow_get(token, path)
    return [_fmt_approval(item) for item in data.get("value", [])]


def action_pending(token: str, environment: str, top: int) -> dict:
    """Aprobaciones donde el usuario es Approver y siguen activas."""
    filter_clause = (
        "properties/userRole eq 'Approver' "
        "and properties/isActive eq 'true' "
        "and properties/isDescending eq 'true'"
    )
    approvals = _list_approvals(token, environment, filter_clause, top)
    urgent = [a for a in approvals if a["isUrgent"]]
    return {
        "action": "pending",
        "environment": environment,
        "total": len(approvals),
        "urgent": len(urgent),
        "approvals": approvals,
        "urgentItems": urgent,
    }


def action_all(token: str, environment: str, top: int) -> dict:
    """Todas las aprobaciones recientes donde el usuario participa
    (Approver o Requestor)."""
    filter_clause = "properties/isDescending eq 'true'"
    approvals = _list_approvals(token, environment, filter_clause, top)
    by_status: dict[str, int] = {}
    for a in approvals:
        s = a["status"]
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "action": "all",
        "environment": environment,
        "total": len(approvals),
        "byStatus": by_status,
        "approvals": approvals,
    }


def action_history(token: str, environment: str, top: int) -> dict:
    """Aprobaciones completadas (ya respondidas)."""
    filter_clause = (
        "properties/userRole eq 'Approver' "
        "and properties/isActive eq 'false' "
        "and properties/isDescending eq 'true'"
    )
    approvals = _list_approvals(token, environment, filter_clause, top)
    return {
        "action": "history",
        "environment": environment,
        "total": len(approvals),
        "approvals": approvals,
    }


def cli() -> None:
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not input_file:
        print(json.dumps(build_error_result("Debes enviar un archivo JSON de entrada.", "MISSING_ARG"), ensure_ascii=False))
        sys.exit(1)

    settings = None
    action = "unknown"
    try:
        raw = _load_input(input_file)
        action = ACTION_ALIASES.get(str(raw.get("action", "pending")), str(raw.get("action", "pending")))
        # FIX importante: capability debe ser "approvals", NO "mail". El
        # runtime ahora mapea "approvals" → RESOURCE_FLOW para que MRRT pida
        # token con aud=service.flow.microsoft.com.
        settings = resolve_graph_settings("approvals", raw)
        user_id = resolve_session_user(settings, raw.get("user"))

        if action == "auth-login":
            data = init_login(settings, user_id)
            result = build_success_result("graph-approvals auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, user_id)
            result = build_success_result("graph-approvals auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, user_id)
        top = int(raw.get("top", 20))
        environment = _resolve_default_environment(token, str(raw.get("environment", "")))

        if action == "pending":
            data = action_pending(token, environment, top)
        elif action == "all":
            data = action_all(token, environment, top)
        elif action == "history":
            data = action_history(token, environment, top)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-approvals ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()