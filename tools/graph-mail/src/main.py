from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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

BASE_URL = "https://graph.microsoft.com/v1.0"
SEL_BASIC = "id,subject,from,receivedDateTime,bodyPreview,importance,isRead,hasAttachments,conversationId,webLink"
SEL_FULL = "id,subject,from,toRecipients,ccRecipients,body,receivedDateTime,importance,isRead,hasAttachments,conversationId,flag,webLink"

# ── Alias map: normaliza variantes que los LLMs suelen enviar ────────────────
ACTION_ALIASES: dict[str, str] = {
    "list": "unread",
    "all": "recent",
    "inbox": "unread",
    "emails": "unread",
    "get": "read",
    "reply_to": "reply",
    "send_mail": "send",
    "new": "send",
    "compose": "send",
    "mark": "mark_read",
    "flag": "mark_read",
    "trash": "delete",
    "borrar": "delete",
    "eliminar": "delete",
    "reenviar": "forward",
    "fwd": "forward",
    "carpetas": "folders",
    "folder": "folders",
    "mover": "move",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _graph(token: str, path: str) -> dict:
    return graph_get_json(f"{BASE_URL}{path}", token)


def _user_path(graph_user_id: str | None) -> str:
    if graph_user_id:
        return f"/users/{urllib.parse.quote(graph_user_id)}"
    return "/me"


def _sender_type(email_addr: str) -> str:
    tec_domains = ["tec.mx", "itesm.mx", "tecvirtual.mx", "tectijuana.mx"]
    if email_addr and any(domain in email_addr.lower() for domain in tec_domains):
        return "interno"
    return "externo"


def _classify(subject: str, preview: str, importance: str) -> str:
    text = f"{subject} {preview}"
    if importance == "high" or re.search(r"urgente|critical|vence hoy|asap|hoy|deadline", text, re.I):
        return "🔴 CRÍTICO"
    if re.search(r"favor de|necesito|solicito|approve|sign|confirm|autoriza|firma|revisa", text, re.I):
        return "🟠 ACCIÓN"
    if re.search(r"decisión|aprobación|autorización|opciones|propongo|¿qué opinas", text, re.I):
        return "🟡 DECISIÓN"
    if re.search(r"FW:|FYI|para tu conocimiento|para tu referencia|forward", text, re.I):
        return "📋 DELEGABLE"
    if re.search(r"en espera|pendiente de|awaiting|esperando respuesta", text, re.I):
        return "⏳ ESPERA"
    return "🔵 INFO"


def _relative(date_str: str) -> str:
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    if mins < 60:
        return f"hace {mins}min"
    if mins < 1440:
        return f"hace {mins // 60}h"
    return f"hace {mins // 1440}d"


def _fmt(msg: dict, classify_it: bool = True) -> dict:
    from_addr = (msg.get("from") or {}).get("emailAddress", {})
    preview = (msg.get("bodyPreview") or "")[:150]
    subject = msg.get("subject") or "(sin asunto)"
    return {
        "id": msg.get("id", ""),
        "conversation": msg.get("conversationId", ""),
        "label": _classify(subject, preview, msg.get("importance", "")) if classify_it else "",
        "subject": subject,
        "from": from_addr.get("name") or from_addr.get("address", "Desconocido"),
        "fromEmail": from_addr.get("address", ""),
        "senderType": _sender_type(from_addr.get("address", "")),
        "relative": _relative(msg["receivedDateTime"]),
        "preview": preview,
        "attach": msg.get("hasAttachments", False),
        "read": msg.get("isRead", False),
        "webLink": msg.get("webLink", ""),
    }


SEMANTIC_MAP = {
    "presupuesto": ["presupuesto", "budget", "gasto", "financiero", "Q1", "Q2", "Q3", "Q4"],
    "convenio": ["convenio", "acuerdo", "contrato", "MOU", "alianza", "firma"],
    "reunión": ["reunión", "meeting", "agenda", "minuta", "sesión"],
    "proyecto": ["proyecto", "iniciativa", "programa", "plan", "estrategia", "roadmap"],
    "gobierno": ["gobierno", "SEP", "CONAHCYT", "federal", "estatal", "secretaría"],
}


def _expand_query(query: str) -> list[str]:
    terms = [query]
    low = query.lower()
    for key, values in SEMANTIC_MAP.items():
        if key in low or any(value.lower() in low for value in values):
            terms.extend(values)
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique.append(term)
    return unique[:8]


def action_unread(token: str, graph_user_id: str, top: int) -> dict:
    query = urllib.parse.urlencode({"$filter": "isRead eq false", "$orderby": "receivedDateTime desc", "$top": top, "$select": SEL_BASIC})
    data = _graph(token, f"{_user_path(graph_user_id)}/mailFolders/inbox/messages?{query}")
    emails = [_fmt(item) for item in data.get("value", [])]
    groups: dict[str, list[dict]] = {}
    for email in emails:
        groups.setdefault(email["label"], []).append(email)
    return {
        "action": "unread",
        "total": len(emails),
        "critical": sum(1 for item in emails if item["label"] == "🔴 CRÍTICO"),
        "interno": sum(1 for item in emails if item["senderType"] == "interno"),
        "externo": sum(1 for item in emails if item["senderType"] == "externo"),
        "byLabel": groups,
        "emails": emails,
    }


def action_digest(token: str, graph_user_id: str, period: str) -> dict:
    days = 1 if period == "day" else 7
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc", "$top": 100, "$select": SEL_BASIC})
    data = _graph(token, f"{_user_path(graph_user_id)}/mailFolders/inbox/messages?{query}")
    emails = [_fmt(item) for item in data.get("value", [])]
    by_label: dict[str, list[dict]] = {}
    senders: dict[str, int] = {}
    for email in emails:
        by_label.setdefault(email["label"], []).append(email)
        senders[email["from"]] = senders.get(email["from"], 0) + 1
    return {
        "action": "digest",
        "period": period,
        "total": len(emails),
        "unread": sum(1 for email in emails if not email["read"]),
        "critical": len(by_label.get("🔴 CRÍTICO", [])),
        "actions": len(by_label.get("🟠 ACCIÓN", [])),
        "decisions": len(by_label.get("🟡 DECISIÓN", [])),
        "topSenders": sorted(senders.items(), key=lambda item: item[1], reverse=True)[:5],
        "actionItems": by_label.get("🔴 CRÍTICO", [])[:5] + by_label.get("🟠 ACCIÓN", [])[:5],
    }


def action_search(token: str, graph_user_id: str, query_text: str, top: int) -> dict:
    if not query_text:
        raise RuntimeError("MISSING_ARG: falta query para action=search")
    results: list[dict] = []
    seen: set[str] = set()
    for term in _expand_query(query_text)[:5]:
        query = urllib.parse.urlencode({"$search": f'"{term}"', "$top": 10, "$select": SEL_BASIC})
        try:
            data = _graph(token, f"{_user_path(graph_user_id)}/messages?{query}")
        except RuntimeError:
            continue
        for item in data.get("value", []):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            email = _fmt(item)
            email["matchedTerm"] = term
            results.append(email)
    return {"action": "search", "query": query_text, "expandedTerms": _expand_query(query_text), "total": len(results), "emails": results[:top]}


def action_read(token: str, graph_user_id: str, message_id: str) -> dict:
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta id para action=read")
    msg = _graph(token, f"{_user_path(graph_user_id)}/messages/{message_id}?$select={SEL_FULL}")
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (msg.get("body") or {}).get("content", ""))).strip()[:3000]
    to_recipients = ", ".join(item.get("emailAddress", {}).get("name") or item.get("emailAddress", {}).get("address", "") for item in (msg.get("toRecipients") or []))
    cc_recipients = ", ".join(item.get("emailAddress", {}).get("name") or item.get("emailAddress", {}).get("address", "") for item in (msg.get("ccRecipients") or []))
    from_addr = (msg.get("from") or {}).get("emailAddress", {})
    return {
        "action": "read",
        "id": msg.get("id"),
        "subject": msg.get("subject"),
        "from": from_addr.get("name") or from_addr.get("address"),
        "fromType": _sender_type(from_addr.get("address", "")),
        "to": to_recipients,
        "cc": cc_recipients,
        "date": _relative(msg["receivedDateTime"]),
        "attach": msg.get("hasAttachments"),
        "webLink": msg.get("webLink", ""),
        "body": body,
        "label": _classify(msg.get("subject", ""), body[:200], msg.get("importance", "")),
    }


def action_tasks(token: str, graph_user_id: str, top: int, days: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {since} and isRead eq false", "$orderby": "receivedDateTime desc", "$top": top, "$select": SEL_FULL})
    data = _graph(token, f"{_user_path(graph_user_id)}/mailFolders/inbox/messages?{query}")
    date_re = re.compile(r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}\s+de\s+\w+|lunes|martes|miércoles|jueves|viernes|sábado|domingo)\b", re.I)
    task_re = re.compile(r"(favor de|por favor|necesito|requiero|solicito|confirma|aprueba|autoriza|firma|envía|manda|prepara|elabora|revisa|agenda|programa|coordina|approve|send|prepare|review|schedule)", re.I)
    tasks: list[dict] = []
    for item in data.get("value", []):
        body = re.sub(r"<[^>]+>", "", (item.get("body") or {}).get("content", ""))
        subject = item.get("subject", "")
        from_addr = (item.get("from") or {}).get("emailAddress", {})
        actions = task_re.findall(body + " " + subject)
        dates = date_re.findall(body + " " + subject)
        if actions:
            tasks.append(
                {
                    "from": from_addr.get("name") or from_addr.get("address", "?"),
                    "subject": subject,
                    "date": _relative(item["receivedDateTime"]),
                    "actions": list(dict.fromkeys(actions))[:3],
                    "deadlines": list(dict.fromkeys(dates))[:3],
                    "messageId": item.get("id"),
                    "preview": (item.get("bodyPreview") or "")[:120],
                }
            )
    return {"action": "tasks", "totalScanned": len(data.get("value", [])), "tasksFound": len(tasks), "tasks": tasks}


def action_pending(token: str, graph_user_id: str, top: int, days: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"$filter": f"sentDateTime ge {since}", "$orderby": "sentDateTime desc", "$top": top, "$select": "id,subject,toRecipients,sentDateTime,conversationId,bodyPreview"})
    sent_items = _graph(token, f"{_user_path(graph_user_id)}/mailFolders/sentItems/messages?{query}")
    pending: list[dict] = []
    for item in sent_items.get("value", []):
        conv_id = item.get("conversationId", "")
        if not conv_id:
            continue
        thread_query = urllib.parse.urlencode({"$filter": f"conversationId eq '{conv_id}'", "$top": 5, "$select": "id,from,receivedDateTime"})
        thread_data = _graph(token, f"{_user_path(graph_user_id)}/messages?{thread_query}")
        sent_dt = datetime.fromisoformat(item["sentDateTime"].replace("Z", "+00:00"))
        has_reply = any(datetime.fromisoformat(msg["receivedDateTime"].replace("Z", "+00:00")) > sent_dt for msg in thread_data.get("value", []))
        if not has_reply:
            to_names = ", ".join(rec.get("emailAddress", {}).get("name") or rec.get("emailAddress", {}).get("address", "") for rec in (item.get("toRecipients") or []))
            days_waiting = int((datetime.now(timezone.utc) - sent_dt).total_seconds() / 86400)
            pending.append(
                {
                    "subject": item.get("subject", ""),
                    "to": to_names,
                    "daysWaiting": days_waiting,
                    "sent": _relative(item["sentDateTime"]),
                    "messageId": item.get("id"),
                    "preview": (item.get("bodyPreview") or "")[:120],
                }
            )
    pending.sort(key=lambda item: item["daysWaiting"], reverse=True)
    return {"action": "pending", "total": len(pending), "pending": pending}


def action_radar(token: str, graph_user_id: str, project: str, top: int) -> dict:
    if not project:
        raise RuntimeError("MISSING_ARG: falta project para action=radar")
    search_result = action_search(token, graph_user_id, project, top)
    emails = search_result["emails"]
    return {
        "action": "radar",
        "project": project,
        "totalEmails": len(emails),
        "unread": sum(1 for item in emails if not item["read"]),
        "critical": sum(1 for item in emails if item["label"] == "🔴 CRÍTICO"),
        "actionItems": [item for item in emails if item["label"] == "🟠 ACCIÓN"][:5],
        "recent": emails[:10],
    }


def _graph_post(token: str, path: str, payload: dict) -> dict:
    """POST JSON to Microsoft Graph, return parsed response."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode()) if resp.status not in (200, 201, 202, 204) or resp.length else (json.loads(resp.read().decode()) if resp.length else {})


def _graph_patch(token: str, path: str, payload: dict) -> dict:
    """PATCH JSON to Microsoft Graph."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw.decode()) if raw else {}


def action_recent(token: str, graph_user_id: str, top: int) -> dict:
    """Lista correos recientes (leídos y no leídos)."""
    query = urllib.parse.urlencode({"$orderby": "receivedDateTime desc", "$top": top, "$select": SEL_BASIC})
    data = _graph(token, f"{_user_path(graph_user_id)}/mailFolders/inbox/messages?{query}")
    return {"action": "recent", "total": len(data.get("value", [])), "emails": [_fmt(m) for m in data.get("value", [])]}


def action_send(token: str, graph_user_id: str, raw: dict) -> dict:
    """Envía un correo nuevo."""
    to = raw.get("to", "")
    subject = raw.get("subject", "(sin asunto)")
    body_text = raw.get("body", "")
    cc_list = raw.get("cc", [])
    if not to:
        raise RuntimeError("MISSING_ARG: falta 'to' para action=send")
    if not body_text:
        raise RuntimeError("MISSING_ARG: falta 'body' para action=send")
    to_recipients = [{"emailAddress": {"address": addr.strip()}} for addr in (to if isinstance(to, list) else to.split(","))]
    cc_recipients = [{"emailAddress": {"address": addr.strip()}} for addr in (cc_list if isinstance(cc_list, list) else cc_list.split(",") if cc_list else [])]
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_text},
        "toRecipients": to_recipients,
    }
    if cc_recipients:
        message["ccRecipients"] = cc_recipients
    _graph_post(token, f"{_user_path(graph_user_id)}/sendMail", {"message": message, "saveToSentItems": True})
    return {"action": "send", "status": "sent", "to": to, "subject": subject}


def action_reply(token: str, graph_user_id: str, message_id: str, body_text: str) -> dict:
    """Responde a un correo existente."""
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=reply")
    if not body_text:
        raise RuntimeError("MISSING_ARG: falta 'body' para action=reply")
    _graph_post(token, f"{_user_path(graph_user_id)}/messages/{message_id}/reply",
                {"message": {"body": {"contentType": "HTML", "content": body_text}}})
    return {"action": "reply", "status": "sent", "messageId": message_id}


def action_mark_read(token: str, graph_user_id: str, message_id: str, is_read: bool = True) -> dict:
    """Marca un correo como leído o no leído."""
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=mark_read")
    _graph_patch(token, f"{_user_path(graph_user_id)}/messages/{message_id}", {"isRead": is_read})
    return {"action": "mark_read", "status": "ok", "messageId": message_id, "isRead": is_read}


def action_folders(token: str, graph_user_id: str, top: int) -> dict:
    """Lista las carpetas del buzón."""
    path_url = f"{_user_path(graph_user_id)}/mailFolders?$top={top}&$select=id,displayName,totalItemCount,unreadItemCount"
    data = graph_get_json(token, path_url)
    folders = [
        {"id": f.get("id"), "name": f.get("displayName"), "total": f.get("totalItemCount", 0), "unread": f.get("unreadItemCount", 0)}
        for f in data.get("value", [])
    ]
    return {"action": "folders", "count": len(folders), "folders": folders}


def action_move(token: str, graph_user_id: str, message_id: str, destination_id: str) -> dict:
    """Mueve un correo a otra carpeta."""
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=move")
    if not destination_id:
        raise RuntimeError("MISSING_ARG: falta 'folderId' para action=move")
    _graph_post(token, f"{_user_path(graph_user_id)}/messages/{message_id}/move", {"destinationId": destination_id})
    return {"action": "move", "status": "ok", "messageId": message_id, "destination": destination_id}


def action_delete(token: str, graph_user_id: str, message_id: str) -> dict:
    """Elimina un correo (mueve a papelera, sin eliminar permanentemente)."""
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=delete")
    _graph_post(token, f"{_user_path(graph_user_id)}/messages/{message_id}/move", {"destinationId": "deleteditems"})
    return {"action": "delete", "status": "ok", "messageId": message_id}


def action_forward(token: str, graph_user_id: str, message_id: str, to_address: str, comment: str = "") -> dict:
    """Reenvía un correo a una dirección."""
    if not message_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=forward")
    if not to_address:
        raise RuntimeError("MISSING_ARG: falta 'to' para action=forward")
    body = {
        "toRecipients": [{"emailAddress": {"address": to_address}}],
        "comment": comment,
    }
    _graph_post(token, f"{_user_path(graph_user_id)}/messages/{message_id}/forward", body)
    return {"action": "forward", "status": "sent", "messageId": message_id, "to": to_address}


def action_suggest(token: str, graph_user_id: str, message_id: str) -> dict:
    email = action_read(token, graph_user_id, message_id)
    first_name = str(email.get("from", "")).split()[0] or "equipo"
    greeting = f"Hola {first_name},"
    closing = "Saludos,\nAgenTEC"
    return {
        "action": "suggest",
        "original": {
            "from": email.get("from"),
            "subject": email.get("subject"),
            "preview": str(email.get("body", ""))[:200],
            "label": email.get("label"),
        },
        "drafts": {
            "brief": f"{greeting}\n\nGracias por el mensaje. Lo reviso y te respondo a la brevedad.\n\n{closing}",
            "detailed": f"{greeting}\n\nGracias por compartir el detalle sobre \"{email.get('subject')}\". Revisaré la información y te responderé con una actualización puntual.\n\n{closing}",
            "delegate": f"{greeting}\n\nGracias por el mensaje. Estoy canalizando este tema con la persona o área responsable para dar seguimiento.\n\n{closing}",
        },
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
        action = ACTION_ALIASES.get(str(raw.get("action", "unread")), str(raw.get("action", "unread")))
        settings = resolve_graph_settings("mail", raw)

        # ── Auth actions (no token required) ────────────────────────────────
        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-mail auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-mail", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-mail auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-mail", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        graph_user_id = str(raw.get("graphUserId", ""))
        top = int(raw.get("top", 20))
        days = int(raw.get("days", 7))
        period = str(raw.get("period", "day"))

        if action == "unread":
            data = action_unread(token, graph_user_id, top)
        elif action == "digest":
            data = action_digest(token, graph_user_id, period)
        elif action == "search":
            data = action_search(token, graph_user_id, str(raw.get("query", "")), top)
        elif action == "read":
            data = action_read(token, graph_user_id, str(raw.get("id", "")))
        elif action == "tasks":
            data = action_tasks(token, graph_user_id, top, days)
        elif action == "pending":
            data = action_pending(token, graph_user_id, top, days)
        elif action == "radar":
            data = action_radar(token, graph_user_id, str(raw.get("project", "")), top)
        elif action == "suggest":
            data = action_suggest(token, graph_user_id, str(raw.get("id", "")))
        elif action == "recent":
            data = action_recent(token, graph_user_id, top)
        elif action == "send":
            data = action_send(token, graph_user_id, raw)
        elif action == "reply":
            data = action_reply(token, graph_user_id, str(raw.get("id", "")), str(raw.get("body", "")))
        elif action == "mark_read":
            data = action_mark_read(token, graph_user_id, str(raw.get("id", "")), bool(raw.get("isRead", True)))
        elif action == "folders":
            data = action_folders(token, graph_user_id, top)
        elif action == "move":
            data = action_move(token, graph_user_id, str(raw.get("id", "")), str(raw.get("folderId", "")))
        elif action == "delete":
            data = action_delete(token, graph_user_id, str(raw.get("id", "")))
        elif action == "forward":
            data = action_forward(token, graph_user_id, str(raw.get("id", "")), str(raw.get("to", "")), str(raw.get("comment", "")))
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-mail ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-mail", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-mail", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
