from __future__ import annotations

import json
import os
import sys
import urllib.parse
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
    graph_get_json,
    init_login,
    poll_login,
    resolve_graph_settings,
    write_result_artifact,
)

BASE_URL = "https://graph.microsoft.com/v1.0"

ACTION_ALIASES: dict[str, str] = {
    "find": "search",
    "lookup": "search",
    "get": "search",
    "list": "list",
    "directory": "list",
    "directorio": "list",
    "profile": "me",
    "perfil": "me",
    "whoami": "me",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _graph(token: str, path: str) -> dict:
    return graph_get_json(f"{BASE_URL}{path}", token)


def _fmt_user(u: dict) -> dict:
    return {
        "id": u.get("id", ""),
        "displayName": u.get("displayName", ""),
        "email": u.get("mail") or u.get("userPrincipalName", ""),
        "jobTitle": u.get("jobTitle", ""),
        "department": u.get("department", ""),
        "officeLocation": u.get("officeLocation", ""),
        "mobilePhone": u.get("mobilePhone", ""),
        "businessPhone": (u.get("businessPhones") or [""])[0],
    }


USER_SELECT = "id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation,mobilePhone,businessPhones"


def action_search(token: str, query: str, top: int) -> dict:
    if not query:
        raise RuntimeError("MISSING_ARG: falta 'query' para action=search")
    # Search by displayName, mail, department
    terms = [
        f"displayName:{query}",
        f"mail:{query}",
    ]
    results: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        params = urllib.parse.urlencode({
            "$search": f'"{term}"',
            "$top": top,
            "$select": USER_SELECT,
        })
        data = _graph(token, f"/users?{params}")
        for u in data.get("value", []):
            uid = u.get("id", "")
            if uid not in seen:
                seen.add(uid)
                results.append(_fmt_user(u))
    return {"action": "search", "query": query, "total": len(results), "users": results[:top]}


def action_list(token: str, department: str, top: int) -> dict:
    params: dict = {"$top": top, "$select": USER_SELECT, "$orderby": "displayName"}
    if department:
        params["$filter"] = f"department eq '{department}'"
    query = urllib.parse.urlencode(params)
    data = _graph(token, f"/users?{query}")
    users = [_fmt_user(u) for u in data.get("value", [])]
    return {"action": "list", "department": department or "all", "total": len(users), "users": users}


def action_me(token: str) -> dict:
    data = _graph(token, f"/me?$select={USER_SELECT}")
    return {"action": "me", "user": _fmt_user(data)}


def action_manager(token: str, query: str) -> dict:
    if not query:
        raise RuntimeError("MISSING_ARG: falta 'query' (usuario) para action=manager")
    # First find the user, then get their manager
    params = urllib.parse.urlencode({
        "$search": f'"displayName:{query}"',
        "$top": 1,
        "$select": "id,displayName,mail",
    })
    data = _graph(token, f"/users?{params}")
    return {
        "action": "manager",
        "subject": _fmt_user(users[0]),
        "manager": _fmt_user(manager_data),
    }


def action_reports(token: str, query: str, top: int) -> dict:
    """Get direct reports of a user."""
    if not query:
        raise RuntimeError("MISSING_ARG: falta 'query' (usuario) para action=reports")
    params = urllib.parse.urlencode({
        "$search": f'"displayName:{query}"',
        "$top": 1,
        "$select": "id,displayName,mail",
    })
    data = _graph(token, f"/users?{params}")
    users = data.get("value", [])
    if not users:
        raise RuntimeError(f"MISSING_ARG: no se encontró usuario con nombre '{query}'")
    uid = users[0]["id"]
    reports_data = _graph(token, f"/users/{uid}/directReports?$select={USER_SELECT}&$top={top}")
    reports = [_fmt_user(r) for r in reports_data.get("value", [])]
    return {
        "action": "reports",
        "subject": _fmt_user(users[0]),
        "totalReports": len(reports),
        "reports": reports,
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
        action = ACTION_ALIASES.get(str(raw.get("action", "search")), str(raw.get("action", "search")))
        settings = resolve_graph_settings("mail", raw)  # reuse mail profile

        # ── Auth actions ──────────────────────────────────────────────────────
        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-users auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-users", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-users auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-users", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        query = str(raw.get("query", ""))
        top = int(raw.get("top", 20))
        department = str(raw.get("department", ""))

        if action == "search":
            data = action_search(token, query, top)
        elif action == "list":
            data = action_list(token, department, top)
        elif action == "me":
            data = action_me(token)
        elif action == "manager":
            data = action_manager(token, query)
        elif action == "reports":
            data = action_reports(token, query, top)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-users ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-users", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-users", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
