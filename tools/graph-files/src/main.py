from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from graph_runtime import (
    GraphSettings,
    build_error_result,
    build_success_result,
    error_type_from_message,
    get_valid_token,
    graph_download,
    graph_get_json,
    init_login,
    poll_login,
    resolve_graph_settings,
    write_result_artifact,
)

BASE_URL = "https://graph.microsoft.com/v1.0"


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _relative(date_str: str) -> str:
    if not date_str:
        return "?"
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    if mins < 60:
        return f"hace {mins}min"
    if mins < 1440:
        return f"hace {mins // 60}h"
    return f"hace {mins // 1440}d"


def _graph(token: str, path: str) -> dict:
    return graph_get_json(f"{BASE_URL}{path}", token)


def _site_drive_prefix(settings: GraphSettings, input_data: dict) -> str:
    drive_mode = str(input_data.get("driveMode") or settings.default_drive_mode or "me")
    if drive_mode != "site":
        graph_user_id = str(input_data.get("graphUserId", ""))
        if graph_user_id:
            return f"/users/{urllib.parse.quote(graph_user_id)}/drive"
        return "/me/drive"

    hostname = str(input_data.get("siteHostname") or settings.site_hostname or "")
    site_path = str(input_data.get("sitePath") or settings.site_path or "")
    if not hostname or not site_path:
        raise RuntimeError("MISSING_ARG: siteHostname y sitePath son requeridos para driveMode=site")
    if not site_path.startswith("/"):
        site_path = f"/{site_path}"
    return f"/sites/{hostname}:{site_path}:/drive"


def _fmt_file(item: dict) -> dict:
    size = item.get("size", 0)
    ext = Path(item.get("name", "")).suffix.lower()
    icon = {
        ".pdf": "📄",
        ".docx": "📝",
        ".xlsx": "📊",
        ".pptx": "📋",
        ".txt": "📃",
        ".md": "📃",
    }.get(ext, "📁")
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "icon": icon,
        "type": "folder" if "folder" in item else "file",
        "sizeKb": round(size / 1024, 1),
        "modified": _relative(item.get("lastModifiedDateTime", "")),
        "modifiedBy": (item.get("lastModifiedBy") or {}).get("user", {}).get("displayName", "?"),
        "webUrl": item.get("webUrl", ""),
        "ext": ext,
    }


def _download_to_temp(token: str, drive_prefix: str, item_id: str) -> tuple[Path, dict]:
    meta = _graph(token, f"{drive_prefix}/items/{item_id}?$select=id,name,size,lastModifiedDateTime,lastModifiedBy,webUrl,file")
    temp_dir = Path(tempfile.mkdtemp(prefix="agentec-graph-files-"))
    file_path = temp_dir / meta.get("name", "file")
    graph_download(f"{BASE_URL}{drive_prefix}/items/{item_id}/content", token, file_path)
    return file_path, meta


def _extract_docx(file_path: Path, max_chars: int) -> str:
    with zipfile.ZipFile(file_path) as archive:
        if "word/document.xml" not in archive.namelist():
            return "[docx sin document.xml]"
        tree = ET.fromstring(archive.read("word/document.xml"))
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        paragraphs = []
        for paragraph in tree.iter(f"{{{ns}}}p"):
            texts = [node.text for node in paragraph.iter(f"{{{ns}}}t") if node.text]
            if texts:
                paragraphs.append("".join(texts))
        return "\n".join(paragraphs)[:max_chars]


def _extract_pptx(file_path: Path, max_chars: int) -> str:
    with zipfile.ZipFile(file_path) as archive:
        slides = sorted([name for name in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml", name)])
        chunks: list[str] = []
        ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        for index, slide in enumerate(slides, start=1):
            tree = ET.fromstring(archive.read(slide))
            texts = [node.text.strip() for node in tree.iter(f"{{{ns}}}t") if node.text and node.text.strip()]
            if texts:
                chunks.append(f"[Slide {index}] {' | '.join(texts)}")
        return "\n".join(chunks)[:max_chars]


def _extract_xlsx(file_path: Path, max_chars: int) -> str:
    with zipfile.ZipFile(file_path) as archive:
        strings: list[str] = []
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        if "xl/sharedStrings.xml" in archive.namelist():
            tree = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = [node.text for node in tree.iter(f"{{{ns}}}t") if node.text]
        rows: list[str] = []
        for sheet_name in sorted([name for name in archive.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", name)])[:3]:
            tree = ET.fromstring(archive.read(sheet_name))
            for row in tree.iter(f"{{{ns}}}row"):
                cells: list[str] = []
                for cell in row.iter(f"{{{ns}}}c"):
                    value = cell.find(f"{{{ns}}}v")
                    if value is None or not value.text:
                        continue
                    if cell.get("t") == "s" and value.text.isdigit():
                        idx = int(value.text)
                        cells.append(strings[idx] if idx < len(strings) else value.text)
                    else:
                        cells.append(value.text)
                if cells:
                    rows.append(" | ".join(cells))
        return "\n".join(rows)[:max_chars] if rows else "[xlsx sin datos de texto]"


def _extract_pdf(file_path: Path, max_chars: int) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        chunks = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            if text and text.strip():
                chunks.append(f"[Página {index}] {text.strip()}")
        return "\n".join(chunks)[:max_chars] if chunks else "[PDF sin texto extraíble]"
    except Exception as exc:  # noqa: BLE001
        return f"[Error extrayendo PDF: {exc}]"


def _extract_text(file_path: Path, max_chars: int) -> str:
    ext = file_path.suffix.lower()
    if ext in {".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".yml", ".yaml"}:
        return file_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    if ext == ".docx":
        return _extract_docx(file_path, max_chars)
    if ext == ".pptx":
        return _extract_pptx(file_path, max_chars)
    if ext in {".xlsx", ".xls"}:
        return _extract_xlsx(file_path, max_chars)
    if ext == ".pdf":
        return _extract_pdf(file_path, max_chars)
    return f"[Tipo {ext or 'desconocido'} no soportado para extracción]"


def action_recent(token: str, drive_prefix: str, top: int) -> dict:
    data = _graph(token, f"{drive_prefix}/recent?$top={top}")
    files = [_fmt_file(item) for item in data.get("value", [])]
    return {"action": "recent", "total": len(files), "files": files}


def action_search(token: str, drive_prefix: str, query_text: str, top: int) -> dict:
    if not query_text:
        raise RuntimeError("MISSING_ARG: falta query para action=search")
    encoded = urllib.parse.quote(query_text)
    data = _graph(token, f"{drive_prefix}/root/search(q='{encoded}')?$top={top}&$select=id,name,size,lastModifiedDateTime,lastModifiedBy,webUrl,folder")
    files = [_fmt_file(item) for item in data.get("value", [])]
    return {"action": "search", "query": query_text, "total": len(files), "files": files}


def action_read(token: str, drive_prefix: str, item_id: str, max_chars: int) -> dict:
    if not item_id:
        raise RuntimeError("MISSING_ARG: falta id para action=read")
    file_path, meta = _download_to_temp(token, drive_prefix, item_id)
    try:
        content = _extract_text(file_path, max_chars)
        return {
            "action": "read",
            "id": item_id,
            "name": meta.get("name", ""),
            "type": file_path.suffix.lower(),
            "sizeKb": round(meta.get("size", 0) / 1024, 1),
            "modified": _relative(meta.get("lastModifiedDateTime", "")),
            "modifiedBy": (meta.get("lastModifiedBy") or {}).get("user", {}).get("displayName", "?"),
            "webUrl": meta.get("webUrl", ""),
            "content": content,
            "extracted": not content.startswith("[Tipo"),
        }
    finally:
        try:
            if file_path.exists():
                file_path.unlink()
            if file_path.parent.exists():
                file_path.parent.rmdir()
        except Exception:
            pass


def action_summarize(token: str, drive_prefix: str, item_id: str, max_chars: int) -> dict:
    data = action_read(token, drive_prefix, item_id, max_chars)
    summary_lines = [
        f"# Resumen: {data['name']}",
        "",
        f"- Tipo: {data['type']}",
        f"- Tamaño: {data['sizeKb']} KB",
        f"- Modificado: {data['modified']} por {data['modifiedBy']}",
        f"- URL: {data['webUrl']}",
        "",
        "## Extracto",
        "",
        data["content"][: min(max_chars, 1500)],
    ]
    return {"action": "summarize", **data, "summary": "\n".join(summary_lines)}


def cli() -> None:
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not input_file:
        print(json.dumps(build_error_result("Debes enviar un archivo JSON de entrada.", "MISSING_ARG"), ensure_ascii=False))
        sys.exit(1)

    settings = None
    action = "unknown"
    try:
        raw = _load_input(input_file)
        action = str(raw.get("action", "recent"))
        settings = resolve_graph_settings("files", raw)

        # ── Auth actions (no token required) ────────────────────────────────
        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-files auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-files", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-files auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-files", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        drive_prefix = _site_drive_prefix(settings, raw)
        top = int(raw.get("top", 20))
        max_chars = int(raw.get("maxChars", 8000))

        if action == "recent":
            data = action_recent(token, drive_prefix, top)
        elif action == "search":
            data = action_search(token, drive_prefix, str(raw.get("query", "")), top)
        elif action == "read":
            data = action_read(token, drive_prefix, str(raw.get("id", "")), max_chars)
        elif action == "summarize":
            data = action_summarize(token, drive_prefix, str(raw.get("id", "")), max_chars)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-files ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-files", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-files", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
