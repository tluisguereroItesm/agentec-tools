from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
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
    "list": "teams",
    "teams": "teams",
    "equipos": "teams",
    "channels": "channels",
    "canales": "channels",
    "messages": "messages",
    "mensajes": "messages",
    "chat": "chats",
    "chats": "chats",
    "send": "send_message",
    "enviar": "send_message",
    "post": "send_message",
    "members": "members",
    "miembros": "members",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _graph(token: str, path: str) -> dict:
    return graph_get_json(f"{BASE_URL}{path}", token)


def _graph_post(token: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method="POST")
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
        raise RuntimeError(f"GRAPH_ERROR: [{exc.code}] {err}") from exc


def action_teams(token: str, top: int) -> dict:
    data = _graph(token, f"/me/joinedTeams?$top={top}&$select=id,displayName,description,visibility")
    teams = [
        {"id": t.get("id", ""), "name": t.get("displayName", ""), "description": (t.get("description") or "")[:100], "visibility": t.get("visibility", "")}
        for t in data.get("value", [])
    ]
    return {"action": "teams", "count": len(teams), "teams": teams}


def action_channels(token: str, team_id: str, top: int) -> dict:
    if not team_id:
        raise RuntimeError("MISSING_ARG: falta 'teamId' para action=channels")
    data = _graph(token, f"/teams/{urllib.parse.quote(team_id)}/channels?$top={top}&$select=id,displayName,description,membershipType")
    channels = [
        {"id": c.get("id", ""), "name": c.get("displayName", ""), "type": c.get("membershipType", ""), "description": (c.get("description") or "")[:100]}
        for c in data.get("value", [])
    ]
    return {"action": "channels", "teamId": team_id, "count": len(channels), "channels": channels}


def action_messages(token: str, team_id: str, channel_id: str, top: int) -> dict:
    if not team_id or not channel_id:
        raise RuntimeError("MISSING_ARG: falta 'teamId' y/o 'channelId' para action=messages")
    data = _graph(token, f"/teams/{urllib.parse.quote(team_id)}/channels/{urllib.parse.quote(channel_id)}/messages?$top={top}")
    msgs = []
    for m in data.get("value", []):
        sender = (m.get("from") or {}).get("user", {})
        body = m.get("body", {})
        msgs.append({
            "id": m.get("id", ""),
            "createdAt": m.get("createdDateTime", ""),
            "from": sender.get("displayName", sender.get("id", "")),
            "text": (body.get("content") or "")[:300],
            "type": body.get("contentType", "text"),
            "replyCount": m.get("replyToId") and 1 or 0,
            "webUrl": m.get("webUrl", ""),
        })
    return {"action": "messages", "teamId": team_id, "channelId": channel_id, "count": len(msgs), "messages": msgs}


def action_send_message(token: str, team_id: str, channel_id: str, content: str) -> dict:
    if not team_id or not channel_id:
        raise RuntimeError("MISSING_ARG: falta 'teamId' y/o 'channelId' para action=send_message")
    if not content:
        raise RuntimeError("MISSING_ARG: falta 'body' para action=send_message")
    result = _graph_post(
        token,
        f"/teams/{urllib.parse.quote(team_id)}/channels/{urllib.parse.quote(channel_id)}/messages",
        {"body": {"contentType": "text", "content": content}},
    )
    return {"action": "send_message", "status": "sent", "id": result.get("id", ""), "webUrl": result.get("webUrl", "")}


def action_chats(token: str, top: int) -> dict:
    data = _graph(token, f"/me/chats?$top={top}&$expand=members&$select=id,chatType,topic,createdDateTime")
    chats = []
    for c in data.get("value", []):
        members = [m.get("displayName", "") for m in (c.get("members") or [])[:5]]
        chats.append({
            "id": c.get("id", ""),
            "type": c.get("chatType", ""),
            "topic": c.get("topic") or ", ".join(members),
            "createdAt": c.get("createdDateTime", ""),
        })
    return {"action": "chats", "count": len(chats), "chats": chats}


def action_members(token: str, team_id: str, top: int) -> dict:
    if not team_id:
        raise RuntimeError("MISSING_ARG: falta 'teamId' para action=members")
    data = _graph(token, f"/teams/{urllib.parse.quote(team_id)}/members?$top={top}")
    members = [
        {"id": m.get("id", ""), "name": m.get("displayName", ""), "email": m.get("email", ""), "roles": m.get("roles", [])}
        for m in data.get("value", [])
    ]
    return {"action": "members", "teamId": team_id, "count": len(members), "members": members}


def cli() -> None:
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not input_file:
        print(json.dumps(build_error_result("Debes enviar un archivo JSON de entrada.", "MISSING_ARG"), ensure_ascii=False))
        sys.exit(1)

    settings = None
    action = "unknown"
    try:
        raw = _load_input(input_file)
        action = ACTION_ALIASES.get(str(raw.get("action", "teams")), str(raw.get("action", "teams")))
        settings = resolve_graph_settings("mail", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-teams auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-teams", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-teams auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-teams", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        top = int(raw.get("top", 20))
        team_id = str(raw.get("teamId", ""))
        channel_id = str(raw.get("channelId", ""))

        if action == "teams":
            data = action_teams(token, top)
        elif action == "channels":
            data = action_channels(token, team_id, top)
        elif action == "messages":
            data = action_messages(token, team_id, channel_id, top)
        elif action == "send_message":
            data = action_send_message(token, team_id, channel_id, str(raw.get("body", "")))
        elif action == "chats":
            data = action_chats(token, top)
        elif action == "members":
            data = action_members(token, team_id, top)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-teams ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-teams", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-teams", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
