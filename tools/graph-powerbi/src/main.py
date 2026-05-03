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
    init_login,
    poll_login,
    resolve_graph_settings,
    write_result_artifact,
)

PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

ACTION_ALIASES: dict[str, str] = {
    "list": "workspaces",
    "workspaces": "workspaces",
    "grupos": "workspaces",
    "espacios": "workspaces",
    "reports": "reports",
    "reportes": "reports",
    "buscar": "reports",
    "search": "reports",
    "dashboards": "dashboards",
    "tableros": "dashboards",
    "datasets": "datasets",
    "datos": "datasets",
    "query": "query",
    "consulta": "query",
    "dax": "query",
    "pregunta": "query",
    "open": "open",
    "abrir": "open",
    "ver": "open",
    "embed": "open",
    "pages": "pages",
    "paginas": "pages",
    "slides": "pages",
    "tiles": "tiles",
    "mosaicos": "tiles",
    "schema": "schema",
    "esquema": "schema",
    "tablas": "schema",
    "columns": "schema",
    "refresh": "refresh",
    "actualizar": "refresh",
    "recargar": "refresh",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _pbi_get(token: str, path: str) -> dict:
    url = f"{PBI_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read()).get("error", {})
            msg = err.get("message", str(exc))
            code_str = err.get("code", "")
        except Exception:
            msg = str(exc)
            code_str = ""
        http_code = exc.code
        if http_code == 401:
            raise RuntimeError("AUTH_ERROR: token de Power BI inválido o expirado")
        if http_code == 403:
            raise RuntimeError(f"PBI_ERROR: [403] Sin permisos — {msg}")
        if http_code == 404:
            raise RuntimeError(f"PBI_ERROR: [404] Recurso no encontrado — {msg}")
        raise RuntimeError(f"PBI_ERROR: [{http_code}] {code_str} {msg}") from exc


def _pbi_post(token: str, path: str, body: dict) -> dict:
    url = f"{PBI_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read()).get("error", {})
            msg = err.get("message", str(exc))
        except Exception:
            msg = str(exc)
        if exc.code == 401:
            raise RuntimeError("AUTH_ERROR: token de Power BI inválido o expirado")
        raise RuntimeError(f"PBI_ERROR: [{exc.code}] {msg}") from exc


def _ws_prefix(workspace_id: str) -> str:
    """URL prefix for workspace-scoped resources, or /myorg for My Workspace."""
    if workspace_id and workspace_id.lower() not in ("me", "my", "mio", ""):
        return f"/groups/{urllib.parse.quote(workspace_id)}"
    return ""


def action_workspaces(token: str, top: int, search: str) -> dict:
    """Lista todos los workspaces a los que tiene acceso el usuario."""
    path = f"/groups?$top={top}"
    if search:
        path += f"&$filter=contains(tolower(name),'{urllib.parse.quote(search.lower())}')"
    data = _pbi_get(token, path)
    workspaces = [
        {
            "id": w.get("id", ""),
            "name": w.get("name", ""),
            "type": w.get("type", "Workspace"),
            "state": w.get("state", ""),
            "isReadOnly": w.get("isReadOnly", False),
        }
        for w in data.get("value", [])
    ]
    return {"action": "workspaces", "count": len(workspaces), "workspaces": workspaces}


def action_reports(token: str, workspace_id: str, search: str, top: int) -> dict:
    """Lista o busca reportes en un workspace."""
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/reports?$top={top}")
    reports = []
    for r in data.get("value", []):
        name = r.get("name", "")
        if search and search.lower() not in name.lower():
            continue
        reports.append({
            "id": r.get("id", ""),
            "name": name,
            "workspaceId": r.get("workspaceId", workspace_id),
            "datasetId": r.get("datasetId", ""),
            "webUrl": r.get("webUrl", ""),
            "embedUrl": r.get("embedUrl", ""),
        })
    return {"action": "reports", "workspaceId": workspace_id, "count": len(reports), "reports": reports}


def action_dashboards(token: str, workspace_id: str, search: str, top: int) -> dict:
    """Lista o busca dashboards en un workspace."""
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/dashboards?$top={top}")
    dashboards = []
    for d in data.get("value", []):
        name = d.get("displayName", "")
        if search and search.lower() not in name.lower():
            continue
        dashboards.append({
            "id": d.get("id", ""),
            "name": name,
            "workspaceId": workspace_id,
            "webUrl": d.get("webUrl", ""),
            "embedUrl": d.get("embedUrl", ""),
            "isReadOnly": d.get("isReadOnly", False),
        })
    return {"action": "dashboards", "workspaceId": workspace_id, "count": len(dashboards), "dashboards": dashboards}


def action_datasets(token: str, workspace_id: str, top: int) -> dict:
    """Lista los datasets de un workspace."""
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/datasets?$top={top}")
    datasets = []
    for d in data.get("value", []):
        datasets.append({
            "id": d.get("id", ""),
            "name": d.get("name", ""),
            "isRefreshable": d.get("isRefreshable", False),
            "isEffectiveIdentityRequired": d.get("isEffectiveIdentityRequired", False),
            "configuredBy": d.get("configuredBy", ""),
            "createdDate": d.get("createdDate", ""),
            "webUrl": d.get("webUrl", ""),
        })
    return {"action": "datasets", "workspaceId": workspace_id, "count": len(datasets), "datasets": datasets}


def action_query(token: str, workspace_id: str, dataset_id: str, dax: str) -> dict:
    """Ejecuta una query DAX contra un dataset y retorna datos reales."""
    if not dataset_id:
        raise RuntimeError("MISSING_ARG: falta 'datasetId' para action=query")
    if not dax:
        raise RuntimeError("MISSING_ARG: falta 'dax' para action=query (ej: EVALUATE SUMMARIZE(Sales, Sales[Year], \"Total\", SUM(Sales[Amount])))")
    prefix = _ws_prefix(workspace_id)
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    result = _pbi_post(token, f"{prefix}/datasets/{urllib.parse.quote(dataset_id)}/executeQueries", body)
    tables = []
    for query_result in result.get("results", []):
        for table in query_result.get("tables", []):
            rows = table.get("rows", [])
            tables.append({"rowCount": len(rows), "rows": rows[:200]})
    return {
        "action": "query",
        "datasetId": dataset_id,
        "workspaceId": workspace_id,
        "dax": dax,
        "results": tables,
        "totalRows": sum(t["rowCount"] for t in tables),
    }


def action_open(token: str, workspace_id: str, report_id: str) -> dict:
    """Obtiene el URL de un reporte para abrirlo en el navegador."""
    if not report_id:
        raise RuntimeError("MISSING_ARG: falta 'reportId' para action=open")
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/reports/{urllib.parse.quote(report_id)}")
    return {
        "action": "open",
        "reportId": report_id,
        "name": data.get("name", ""),
        "webUrl": data.get("webUrl", ""),
        "embedUrl": data.get("embedUrl", ""),
        "datasetId": data.get("datasetId", ""),
        "instruction": f"Haz clic en el enlace para abrir el reporte en Power BI: {data.get('webUrl', '')}",
    }


def action_pages(token: str, workspace_id: str, report_id: str) -> dict:
    """Lista las páginas/pestañas de un reporte."""
    if not report_id:
        raise RuntimeError("MISSING_ARG: falta 'reportId' para action=pages")
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/reports/{urllib.parse.quote(report_id)}/pages")
    pages = [
        {
            "name": p.get("name", ""),
            "displayName": p.get("displayName", ""),
            "order": p.get("order", 0),
            "visibility": p.get("visibility", ""),
        }
        for p in data.get("value", [])
    ]
    pages.sort(key=lambda x: x["order"])
    return {"action": "pages", "reportId": report_id, "count": len(pages), "pages": pages}


def action_tiles(token: str, workspace_id: str, dashboard_id: str) -> dict:
    """Lista los mosaicos/tiles de un dashboard."""
    if not dashboard_id:
        raise RuntimeError("MISSING_ARG: falta 'dashboardId' para action=tiles")
    prefix = _ws_prefix(workspace_id)
    data = _pbi_get(token, f"{prefix}/dashboards/{urllib.parse.quote(dashboard_id)}/tiles")
    tiles = [
        {
            "id": t.get("id", ""),
            "title": t.get("title", "(sin título)"),
            "reportId": t.get("reportId", ""),
            "datasetId": t.get("datasetId", ""),
            "embedUrl": t.get("embedUrl", ""),
            "rowSpan": t.get("rowSpan", 1),
            "colSpan": t.get("colSpan", 1),
        }
        for t in data.get("value", [])
    ]
    return {"action": "tiles", "dashboardId": dashboard_id, "count": len(tiles), "tiles": tiles}


def action_schema(token: str, workspace_id: str, dataset_id: str) -> dict:
    """Obtiene el esquema (tablas y columnas) de un dataset para formular queries DAX."""
    if not dataset_id:
        raise RuntimeError("MISSING_ARG: falta 'datasetId' para action=schema")
    prefix = _ws_prefix(workspace_id)
    # Use the tables REST endpoint
    data = _pbi_get(token, f"{prefix}/datasets/{urllib.parse.quote(dataset_id)}/tables")
    tables = []
    for t in data.get("value", []):
        columns = [
            {"name": c.get("name", ""), "dataType": c.get("dataType", ""), "isHidden": c.get("isHidden", False)}
            for c in t.get("columns", [])
            if not c.get("isHidden", False)
        ]
        measures = [
            {"name": m.get("name", ""), "expression": (m.get("expression") or "")[:100]}
            for m in t.get("measures", [])
        ]
        tables.append({
            "name": t.get("name", ""),
            "columns": columns,
            "measures": measures,
            "isHidden": t.get("isHidden", False),
        })
    return {
        "action": "schema",
        "datasetId": dataset_id,
        "workspaceId": workspace_id,
        "tableCount": len(tables),
        "tables": [t for t in tables if not t["isHidden"]],
        "tip": "Usa action=query con DAX como: EVALUATE SUMMARIZE(<Table>, <GroupByCol>, \"Metric\", <Expression>)",
    }


def action_refresh(token: str, workspace_id: str, dataset_id: str, trigger: bool) -> dict:
    """Consulta el historial de refreshes o lanza un refresh del dataset."""
    if not dataset_id:
        raise RuntimeError("MISSING_ARG: falta 'datasetId' para action=refresh")
    prefix = _ws_prefix(workspace_id)
    if trigger:
        _pbi_post(token, f"{prefix}/datasets/{urllib.parse.quote(dataset_id)}/refreshes", {})
        return {"action": "refresh", "status": "triggered", "datasetId": dataset_id}
    data = _pbi_get(token, f"{prefix}/datasets/{urllib.parse.quote(dataset_id)}/refreshes?$top=5")
    history = [
        {
            "id": r.get("id", r.get("requestId", "")),
            "status": r.get("status", ""),
            "startTime": r.get("startTime", ""),
            "endTime": r.get("endTime", ""),
            "error": (r.get("serviceExceptionJson") or ""),
        }
        for r in data.get("value", [])
    ]
    last_status = history[0]["status"] if history else "unknown"
    return {
        "action": "refresh",
        "datasetId": dataset_id,
        "lastStatus": last_status,
        "history": history,
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
        action = ACTION_ALIASES.get(str(raw.get("action", "workspaces")), str(raw.get("action", "workspaces")))
        settings = resolve_graph_settings("powerbi", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-powerbi auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-powerbi", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-powerbi auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-powerbi", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        workspace_id = str(raw.get("workspaceId", ""))
        top = int(raw.get("top", 50))
        search = str(raw.get("search", raw.get("query", "")))

        if action == "workspaces":
            data = action_workspaces(token, top, search)
        elif action == "reports":
            data = action_reports(token, workspace_id, search, top)
        elif action == "dashboards":
            data = action_dashboards(token, workspace_id, search, top)
        elif action == "datasets":
            data = action_datasets(token, workspace_id, top)
        elif action == "query":
            data = action_query(token, workspace_id, str(raw.get("datasetId", "")), str(raw.get("dax", "")))
        elif action == "open":
            data = action_open(token, workspace_id, str(raw.get("reportId", "")))
        elif action == "pages":
            data = action_pages(token, workspace_id, str(raw.get("reportId", "")))
        elif action == "tiles":
            data = action_tiles(token, workspace_id, str(raw.get("dashboardId", "")))
        elif action == "schema":
            data = action_schema(token, workspace_id, str(raw.get("datasetId", "")))
        elif action == "refresh":
            data = action_refresh(token, workspace_id, str(raw.get("datasetId", "")), bool(raw.get("trigger", False)))
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-powerbi ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-powerbi", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-powerbi", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
