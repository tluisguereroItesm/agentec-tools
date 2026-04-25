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

BASE_URL = "https://graph.microsoft.com/v1.0"

ACTION_ALIASES: dict[str, str] = {
    "find": "search",
    "buscar": "search",
    "lookup": "search",
    "sites": "list-sites",
    "sitios": "list-sites",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _graph_search(token: str, payload: dict) -> dict:
    """POST to Microsoft Search API."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/search/query",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _fmt_hit(hit: dict) -> dict:
    resource = hit.get("resource", {})
    fields = hit.get("fields", {})
    return {
        "id": resource.get("id", ""),
        "name": resource.get("name") or fields.get("fileName") or resource.get("displayName", ""),
        "webUrl": resource.get("webUrl", ""),
        "summary": hit.get("summary", ""),
        "score": hit.get("rank", 0),
        "createdBy": (resource.get("createdBy") or {}).get("user", {}).get("displayName", ""),
        "lastModified": resource.get("lastModifiedDateTime", ""),
        "size": resource.get("size", 0),
        "fileType": resource.get("file", {}).get("mimeType", ""),
        "parentPath": (resource.get("parentReference") or {}).get("path", ""),
        "siteId": (resource.get("parentReference") or {}).get("siteId", ""),
    }


def action_search(token: str, query: str, top: int, content_sources: list[str]) -> dict:
    if not query:
        raise RuntimeError("MISSING_ARG: falta 'query' para action=search")

    entity_types = ["driveItem", "listItem", "site"]
    payload = {
        "requests": [
            {
                "entityTypes": entity_types,
                "query": {"queryString": query},
                "size": top,
                "fields": ["name", "webUrl", "createdBy", "lastModifiedDateTime", "size", "parentReference"],
            }
        ]
    }
    if content_sources:
        payload["requests"][0]["contentSources"] = content_sources

    data = _graph_search(token, payload)
    hits_containers = data.get("value", [{}])[0].get("hitsContainers", [])
    results: list[dict] = []
    for container in hits_containers:
        for hit in container.get("hits", []):
            results.append(_fmt_hit(hit))

    return {
        "action": "search",
        "query": query,
        "total": len(results),
        "results": results,
    }


def action_list_sites(token: str, top: int) -> dict:
    params = urllib.parse.urlencode({"$top": top, "$select": "id,displayName,webUrl,description,createdDateTime"})
    data = graph_get_json(f"{BASE_URL}/sites?search=*&{params}", token)
    sites = [
        {
            "id": s.get("id", ""),
            "displayName": s.get("displayName", ""),
            "webUrl": s.get("webUrl", ""),
            "description": s.get("description", ""),
            "created": s.get("createdDateTime", ""),
        }
        for s in data.get("value", [])
    ]
    return {"action": "list-sites", "total": len(sites), "sites": sites}


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
        settings = resolve_graph_settings("files", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-sharepoint-search auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-sharepoint-search", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-sharepoint-search auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-sharepoint-search", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        query = str(raw.get("query", ""))
        top = int(raw.get("top", 20))
        content_sources = raw.get("contentSources", [])

        if action == "search":
            data = action_search(token, query, top, content_sources)
        elif action == "list-sites":
            data = action_list_sites(token, top)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-sharepoint-search ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-sharepoint-search", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-sharepoint-search", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
