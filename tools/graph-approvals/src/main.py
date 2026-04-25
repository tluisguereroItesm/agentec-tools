from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from graph_runtime import (
    build_error_result,
    build_success_result,
    error_type_from_message,
    get_valid_token,
    graph_get_json,
    init_login,
    poll_login,
    resolve_graph_settings,
    write_result_artifact,
)

# Power Automate approvals API
APPROVALS_BASE = "https://consent.azure.com/api"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

ACTION_ALIASES: dict[str, str] = {
    "list": "pending",
    "pendientes": "pending",
    "todas": "all",
    "historial": "history",
    "history": "history",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _approvals_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{APPROVALS_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        code = exc.code
        message = body.get("error", {}).get("message", str(exc))
        raise RuntimeError(f"APPROVALS_ERROR: [{code}] {message}")


def _relative(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        days = int((datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        if days == 0:
            return "hoy"
        if days == 1:
            return "ayer"
        return f"hace {days} días"
    except Exception:
        return date_str


def _fmt_approval(a: dict) -> dict:
    return {
        "id": a.get("approvalId") or a.get("id", ""),
        "title": a.get("title", ""),
        "requestor": (a.get("requestorTeamsMemberInfo") or {}).get("displayName", "") or a.get("requestorEmailAddress", ""),
        "status": a.get("status", ""),
        "created": _relative(a.get("createdDateTime", "")),
        "dueDate": _relative(a.get("dueDateTime", "")) if a.get("dueDateTime") else "",
        "isUrgent": a.get("isUrgent", False),
        "itemDetails": a.get("itemDetails", ""),
        "responseOptions": [r.get("label", "") for r in (a.get("responseOptions") or [])],
    }


def action_pending(token: str, top: int) -> dict:
    """Aprobaciones pendientes de acción del usuario."""
    params = urllib.parse.urlencode({"status": "Pending", "top": top})
    data = _approvals_get(token, f"/approvals?{params}")
    approvals = [_fmt_approval(a) for a in data.get("value", [])]
    urgent = [a for a in approvals if a["isUrgent"]]
    return {
        "action": "pending",
        "total": len(approvals),
        "urgent": len(urgent),
        "approvals": approvals,
        "urgentItems": urgent,
    }


def action_all(token: str, top: int) -> dict:
    """Todas las aprobaciones recientes."""
    params = urllib.parse.urlencode({"top": top})
    data = _approvals_get(token, f"/approvals?{params}")
    approvals = [_fmt_approval(a) for a in data.get("value", [])]
    by_status: dict[str, int] = {}
    for a in approvals:
        s = a["status"]
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "action": "all",
        "total": len(approvals),
        "byStatus": by_status,
        "approvals": approvals,
    }


def action_history(token: str, top: int) -> dict:
    """Historial de aprobaciones completadas."""
    params = urllib.parse.urlencode({"status": "Completed", "top": top})
    data = _approvals_get(token, f"/approvals?{params}")
    approvals = [_fmt_approval(a) for a in data.get("value", [])]
    return {"action": "history", "total": len(approvals), "approvals": approvals}


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
        settings = resolve_graph_settings("mail", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-approvals auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-approvals auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-approvals", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        top = int(raw.get("top", 20))

        if action == "pending":
            data = action_pending(token, top)
        elif action == "all":
            data = action_all(token, top)
        elif action == "history":
            data = action_history(token, top)
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
