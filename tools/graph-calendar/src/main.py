from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
    "list": "today",
    "agenda": "today",
    "hoy": "today",
    "week": "week",
    "semana": "week",
    "month": "month",
    "mes": "month",
    "event": "read",
    "get": "read",
    "create": "create",
    "nuevo": "create",
    "new": "create",
    "agendar": "create",
    "update": "update",
    "modify": "update",
    "modificar": "update",
    "delete": "delete",
    "cancel": "delete",
    "cancelar": "delete",
    "free": "availability",
    "disponible": "availability",
    "disponibilidad": "availability",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _graph(token: str, path: str) -> dict:
    return graph_get_json(f"{BASE_URL}{path}", token)


def _user_path(graph_user_id: str | None) -> str:
    if graph_user_id:
        return f"/users/{urllib.parse.quote(graph_user_id)}"
    return "/me"


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
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes).get("error", {}).get("message", str(exc))
        except Exception:
            err = str(exc)
        raise RuntimeError(f"GRAPH_ERROR: [{exc.code}] {err}") from exc


def _graph_patch(token: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method="PATCH")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes).get("error", {}).get("message", str(exc))
        except Exception:
            err = str(exc)
        raise RuntimeError(f"GRAPH_ERROR: [{exc.code}] {err}") from exc


def _graph_delete(token: str, path: str) -> None:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 204:
            raise RuntimeError(f"GRAPH_ERROR: [{exc.code}] {exc}") from exc


def _fmt_event(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    organizer = (ev.get("organizer") or {}).get("emailAddress", {})
    attendees = [
        {"name": a.get("emailAddress", {}).get("name", ""), "email": a.get("emailAddress", {}).get("address", "")}
        for a in (ev.get("attendees") or [])
    ]
    return {
        "id": ev.get("id", ""),
        "subject": ev.get("subject", "(sin título)"),
        "start": start.get("dateTime", ""),
        "startTz": start.get("timeZone", ""),
        "end": end.get("dateTime", ""),
        "endTz": end.get("timeZone", ""),
        "location": (ev.get("location") or {}).get("displayName", ""),
        "organizer": organizer.get("name", organizer.get("address", "")),
        "attendeesCount": len(attendees),
        "attendees": attendees[:10],
        "isOnline": ev.get("isOnlineMeeting", False),
        "joinUrl": ev.get("onlineMeeting", {}).get("joinUrl", "") if ev.get("onlineMeeting") else "",
        "bodyPreview": (ev.get("bodyPreview") or "")[:200],
        "webLink": ev.get("webLink", ""),
        "recurrence": ev.get("recurrence") is not None,
        "sensitivity": ev.get("sensitivity", "normal"),
    }


def _range_params(days_ahead: int = 7, days_back: int = 0) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def action_today(token: str, user_path: str, top: int) -> dict:
    start, end = _range_params(days_ahead=1, days_back=0)
    url = (
        f"{user_path}/calendarView?startDateTime={start}&endDateTime={end}"
        f"&$top={top}&$orderby=start/dateTime"
        f"&$select=id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeeting,bodyPreview,webLink,recurrence,sensitivity"
    )
    data = _graph(token, url)
    events = [_fmt_event(e) for e in data.get("value", [])]
    return {"action": "today", "count": len(events), "events": events}


def action_week(token: str, user_path: str, top: int) -> dict:
    start, end = _range_params(days_ahead=7, days_back=0)
    url = (
        f"{user_path}/calendarView?startDateTime={start}&endDateTime={end}"
        f"&$top={top}&$orderby=start/dateTime"
        f"&$select=id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeeting,bodyPreview,webLink,recurrence,sensitivity"
    )
    data = _graph(token, url)
    events = [_fmt_event(e) for e in data.get("value", [])]
    return {"action": "week", "count": len(events), "events": events}


def action_month(token: str, user_path: str, top: int) -> dict:
    start, end = _range_params(days_ahead=30, days_back=0)
    url = (
        f"{user_path}/calendarView?startDateTime={start}&endDateTime={end}"
        f"&$top={min(top, 100)}&$orderby=start/dateTime"
        f"&$select=id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeeting,bodyPreview,webLink,recurrence,sensitivity"
    )
    data = _graph(token, url)
    events = [_fmt_event(e) for e in data.get("value", [])]
    return {"action": "month", "count": len(events), "events": events}


def action_read(token: str, user_path: str, event_id: str) -> dict:
    if not event_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=read")
    data = _graph(token, f"{user_path}/events/{urllib.parse.quote(event_id)}")
    return {"action": "read", "event": _fmt_event(data)}


def action_create(token: str, user_path: str, raw: dict) -> dict:
    subject = str(raw.get("subject", ""))
    if not subject:
        raise RuntimeError("MISSING_ARG: falta 'subject' para action=create")
    start_dt = str(raw.get("start", ""))
    end_dt = str(raw.get("end", ""))
    if not start_dt or not end_dt:
        raise RuntimeError("MISSING_ARG: falta 'start' y/o 'end' para action=create (formato ISO 8601)")
    tz = str(raw.get("timezone", "America/Monterrey"))
    body: dict = {
        "subject": subject,
        "start": {"dateTime": start_dt, "timeZone": tz},
        "end": {"dateTime": end_dt, "timeZone": tz},
    }
    if raw.get("body"):
        body["body"] = {"contentType": "text", "content": str(raw["body"])}
    if raw.get("location"):
        body["location"] = {"displayName": str(raw["location"])}
    if raw.get("attendees"):
        body["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in (raw["attendees"] if isinstance(raw["attendees"], list) else [raw["attendees"]])
        ]
    if raw.get("isOnline"):
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    result = _graph_post(token, f"{user_path}/events", body)
    return {"action": "create", "status": "created", "id": result.get("id", ""), "webLink": result.get("webLink", ""), "subject": result.get("subject", "")}


def action_update(token: str, user_path: str, event_id: str, raw: dict) -> dict:
    if not event_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=update")
    patch: dict = {}
    if raw.get("subject"):
        patch["subject"] = str(raw["subject"])
    if raw.get("start") and raw.get("end"):
        tz = str(raw.get("timezone", "America/Monterrey"))
        patch["start"] = {"dateTime": str(raw["start"]), "timeZone": tz}
        patch["end"] = {"dateTime": str(raw["end"]), "timeZone": tz}
    if raw.get("location"):
        patch["location"] = {"displayName": str(raw["location"])}
    if raw.get("body"):
        patch["body"] = {"contentType": "text", "content": str(raw["body"])}
    if not patch:
        raise RuntimeError("MISSING_ARG: no se especificó ningún campo para actualizar")
    _graph_patch(token, f"{user_path}/events/{urllib.parse.quote(event_id)}", patch)
    return {"action": "update", "status": "updated", "id": event_id}


def action_delete(token: str, user_path: str, event_id: str) -> dict:
    if not event_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=delete")
    _graph_delete(token, f"{user_path}/events/{urllib.parse.quote(event_id)}")
    return {"action": "delete", "status": "cancelled", "id": event_id}


def action_availability(token: str, user_path: str, raw: dict) -> dict:
    """Encuentra espacios libres en el calendario."""
    start, end = _range_params(days_ahead=int(raw.get("days", 5)), days_back=0)
    url = (
        f"{user_path}/calendarView?startDateTime={start}&endDateTime={end}"
        f"&$top=50&$orderby=start/dateTime"
        f"&$select=subject,start,end"
    )
    data = _graph(token, url)
    busy = [(e["start"]["dateTime"], e["end"]["dateTime"]) for e in data.get("value", [])]
    return {
        "action": "availability",
        "daysChecked": raw.get("days", 5),
        "busySlots": len(busy),
        "busy": [{"start": s, "end": e} for s, e in busy],
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
        action = ACTION_ALIASES.get(str(raw.get("action", "today")), str(raw.get("action", "today")))
        settings = resolve_graph_settings("calendar", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-calendar auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-calendar", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-calendar auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-calendar", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        user_path = _user_path(str(raw.get("graphUserId", "")))
        top = int(raw.get("top", 20))

        if action == "today":
            data = action_today(token, user_path, top)
        elif action == "week":
            data = action_week(token, user_path, top)
        elif action == "month":
            data = action_month(token, user_path, top)
        elif action == "read":
            data = action_read(token, user_path, str(raw.get("id", "")))
        elif action == "create":
            data = action_create(token, user_path, raw)
        elif action == "update":
            data = action_update(token, user_path, str(raw.get("id", "")), raw)
        elif action == "delete":
            data = action_delete(token, user_path, str(raw.get("id", "")))
        elif action == "availability":
            data = action_availability(token, user_path, raw)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-calendar ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-calendar", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-calendar", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
