from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
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

# Power Automate / Power Platform APIs
FLOW_BASE = "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"

ACTION_ALIASES: dict[str, str] = {
    "list": "list",
    "flows": "list",
    "listar": "list",
    "mis": "list",
    "get": "read",
    "detail": "read",
    "detalle": "read",
    "run": "trigger",
    "ejecutar": "trigger",
    "trigger": "trigger",
    "runs": "runs",
    "history": "runs",
    "historial": "runs",
    "enable": "enable",
    "habilitar": "enable",
    "disable": "disable",
    "deshabilitar": "disable",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _flow_get(token: str, path: str, environment: str = "~default") -> dict:
    url = f"{FLOW_BASE}/environments/{urllib.parse.quote(environment)}{path}?api-version=2016-11-01"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read()).get("error", {}).get("message", str(exc))
        except Exception:
            err = str(exc)
        raise RuntimeError(f"FLOW_ERROR: [{exc.code}] {err}") from exc


def _flow_post(token: str, path: str, body: dict, environment: str = "~default") -> dict:
    url = f"{FLOW_BASE}/environments/{urllib.parse.quote(environment)}{path}?api-version=2016-11-01"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read()).get("error", {}).get("message", str(exc))
        except Exception:
            err = str(exc)
        raise RuntimeError(f"FLOW_ERROR: [{exc.code}] {err}") from exc


def _fmt_flow(f: dict) -> dict:
    props = f.get("properties", {})
    return {
        "id": f.get("name", ""),
        "displayName": props.get("displayName", ""),
        "state": props.get("state", ""),
        "createdTime": props.get("createdTime", ""),
        "lastModifiedTime": props.get("lastModifiedTime", ""),
        "triggerType": (props.get("definition", {}).get("triggers", {}) or {}).keys().__iter__().__next__() if props.get("definition", {}).get("triggers") else "",
    }


def action_list(token: str, environment: str, top: int) -> dict:
    data = _flow_get(token, f"/flows?$top={top}", environment)
    flows = [_fmt_flow(f) for f in data.get("value", [])]
    return {"action": "list", "environment": environment, "count": len(flows), "flows": flows}


def action_read(token: str, flow_id: str, environment: str) -> dict:
    if not flow_id:
        raise RuntimeError("MISSING_ARG: falta 'flowId' para action=read")
    data = _flow_get(token, f"/flows/{urllib.parse.quote(flow_id)}", environment)
    return {"action": "read", "flow": _fmt_flow(data)}


def action_runs(token: str, flow_id: str, environment: str, top: int) -> dict:
    if not flow_id:
        raise RuntimeError("MISSING_ARG: falta 'flowId' para action=runs")
    data = _flow_get(token, f"/flows/{urllib.parse.quote(flow_id)}/runs?$top={top}", environment)
    runs = []
    for r in data.get("value", []):
        props = r.get("properties", {})
        runs.append({
            "id": r.get("name", ""),
            "status": props.get("status", ""),
            "startTime": props.get("startTime", ""),
            "endTime": props.get("endTime", ""),
            "error": (props.get("error") or {}).get("message", ""),
        })
    return {"action": "runs", "flowId": flow_id, "count": len(runs), "runs": runs}


def action_trigger(token: str, flow_id: str, environment: str, body: dict) -> dict:
    """Ejecuta un flow que tenga trigger manual."""
    if not flow_id:
        raise RuntimeError("MISSING_ARG: falta 'flowId' para action=trigger")
    _flow_post(token, f"/flows/{urllib.parse.quote(flow_id)}/triggers/manual/run", body or {}, environment)
    return {"action": "trigger", "status": "triggered", "flowId": flow_id}


def action_enable(token: str, flow_id: str, environment: str) -> dict:
    if not flow_id:
        raise RuntimeError("MISSING_ARG: falta 'flowId' para action=enable")
    _flow_post(token, f"/flows/{urllib.parse.quote(flow_id)}/start", {}, environment)
    return {"action": "enable", "status": "enabled", "flowId": flow_id}


def action_disable(token: str, flow_id: str, environment: str) -> dict:
    if not flow_id:
        raise RuntimeError("MISSING_ARG: falta 'flowId' para action=disable")
    _flow_post(token, f"/flows/{urllib.parse.quote(flow_id)}/stop", {}, environment)
    return {"action": "disable", "status": "disabled", "flowId": flow_id}


def cli() -> None:
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not input_file:
        print(json.dumps(build_error_result("Debes enviar un archivo JSON de entrada.", "MISSING_ARG"), ensure_ascii=False))
        sys.exit(1)

    settings = None
    action = "unknown"
    try:
        raw = _load_input(input_file)
        action = ACTION_ALIASES.get(str(raw.get("action", "list")), str(raw.get("action", "list")))
        settings = resolve_graph_settings("mail", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-flows auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-flows", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-flows auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-flows", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        environment = str(raw.get("environment", "~default"))
        flow_id = str(raw.get("flowId", ""))
        top = int(raw.get("top", 20))

        if action == "list":
            data = action_list(token, environment, top)
        elif action == "read":
            data = action_read(token, flow_id, environment)
        elif action == "runs":
            data = action_runs(token, flow_id, environment, top)
        elif action == "trigger":
            data = action_trigger(token, flow_id, environment, raw.get("triggerBody") or {})
        elif action == "enable":
            data = action_enable(token, flow_id, environment)
        elif action == "disable":
            data = action_disable(token, flow_id, environment)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-flows ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-flows", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-flows", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
