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
    "upload": "upload",
    "subir": "upload",
    "write": "upload",
    "create": "create_folder",
    "mkdir": "create_folder",
    "carpeta": "create_folder",
    "folder": "create_folder",
    "rename": "rename",
    "move": "move",
    "mover": "move",
    "copy": "copy",
    "copiar": "copy",
    "delete": "delete",
    "borrar": "delete",
    "eliminar": "delete",
    "trash": "delete",
    "share": "share",
    "compartir": "share",
    "link": "share",
}


def _load_input(path_arg: str) -> dict:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _me_drive(user_id: str | None) -> str:
    if user_id:
        return f"/users/{urllib.parse.quote(user_id)}/drive"
    return "/me/drive"


def _graph_put(token: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read()).get("error", {}).get("message", str(exc))
        except Exception:
            err = str(exc)
        raise RuntimeError(f"GRAPH_ERROR: [{exc.code}] {err}") from exc


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
        try:
            err = json.loads(exc.read()).get("error", {}).get("message", str(exc))
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


def action_upload(token: str, drive_path: str, raw: dict) -> dict:
    """Sube un archivo local a OneDrive."""
    local_path = str(raw.get("localPath", ""))
    remote_path = str(raw.get("remotePath", ""))
    if not local_path or not remote_path:
        raise RuntimeError("MISSING_ARG: falta 'localPath' y/o 'remotePath' para action=upload")
    file_bytes = Path(local_path).read_bytes()
    encoded = urllib.parse.quote(remote_path.lstrip("/"), safe="/")
    result = _graph_put(token, f"{drive_path}/root:/{encoded}:/content", file_bytes)
    return {
        "action": "upload",
        "status": "uploaded",
        "id": result.get("id", ""),
        "name": result.get("name", ""),
        "size": result.get("size", 0),
        "webUrl": result.get("webUrl", ""),
    }


def action_create_folder(token: str, drive_path: str, raw: dict) -> dict:
    """Crea una carpeta en OneDrive."""
    parent = str(raw.get("parent", "root"))
    name = str(raw.get("name", ""))
    if not name:
        raise RuntimeError("MISSING_ARG: falta 'name' para action=create_folder")
    if parent == "root":
        path = f"{drive_path}/root/children"
    else:
        encoded = urllib.parse.quote(parent.lstrip("/"), safe="/")
        path = f"{drive_path}/root:/{encoded}:/children"
    result = _graph_post(token, path, {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"})
    return {
        "action": "create_folder",
        "status": "created",
        "id": result.get("id", ""),
        "name": result.get("name", ""),
        "webUrl": result.get("webUrl", ""),
    }


def action_rename(token: str, drive_path: str, item_id: str, new_name: str) -> dict:
    if not item_id or not new_name:
        raise RuntimeError("MISSING_ARG: falta 'id' y/o 'name' para action=rename")
    result = _graph_patch(token, f"{drive_path}/items/{item_id}", {"name": new_name})
    return {"action": "rename", "status": "renamed", "id": item_id, "newName": result.get("name", new_name)}


def action_move(token: str, drive_path: str, item_id: str, destination_id: str) -> dict:
    if not item_id or not destination_id:
        raise RuntimeError("MISSING_ARG: falta 'id' y/o 'destinationId' para action=move")
    result = _graph_patch(token, f"{drive_path}/items/{item_id}", {"parentReference": {"id": destination_id}})
    return {"action": "move", "status": "moved", "id": result.get("id", item_id), "name": result.get("name", "")}


def action_copy(token: str, drive_path: str, item_id: str, destination_id: str, new_name: str = "") -> dict:
    if not item_id or not destination_id:
        raise RuntimeError("MISSING_ARG: falta 'id' y/o 'destinationId' para action=copy")
    body: dict = {"parentReference": {"id": destination_id}}
    if new_name:
        body["name"] = new_name
    _graph_post(token, f"{drive_path}/items/{item_id}/copy", body)
    return {"action": "copy", "status": "copy_initiated", "sourceId": item_id, "destinationId": destination_id}


def action_delete(token: str, drive_path: str, item_id: str) -> dict:
    if not item_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=delete")
    _graph_delete(token, f"{drive_path}/items/{item_id}")
    return {"action": "delete", "status": "deleted", "id": item_id}


def action_share(token: str, drive_path: str, item_id: str, raw: dict) -> dict:
    """Crea un enlace para compartir un archivo."""
    if not item_id:
        raise RuntimeError("MISSING_ARG: falta 'id' para action=share")
    link_type = str(raw.get("linkType", "view"))  # view | edit | embed
    scope = str(raw.get("scope", "organization"))  # organization | anonymous
    result = _graph_post(token, f"{drive_path}/items/{item_id}/createLink", {"type": link_type, "scope": scope})
    link = result.get("link", {})
    return {
        "action": "share",
        "status": "link_created",
        "type": link.get("type", link_type),
        "scope": link.get("scope", scope),
        "webUrl": link.get("webUrl", ""),
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
        action = ACTION_ALIASES.get(str(raw.get("action", "")), str(raw.get("action", "")))
        if not action:
            raise RuntimeError("MISSING_ARG: falta 'action'")
        settings = resolve_graph_settings("files", raw)

        if action == "auth-login":
            data = init_login(settings, raw.get("user"))
            result = build_success_result("graph-files-write auth-login iniciado", data, settings)
            result["artifactPath"] = write_result_artifact("graph-files-write", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return
        if action == "auth-poll":
            data = poll_login(settings, raw.get("user"))
            result = build_success_result("graph-files-write auth-poll", data, settings)
            result["artifactPath"] = write_result_artifact("graph-files-write", action, result)
            print(json.dumps(result, ensure_ascii=False))
            return

        token = get_valid_token(settings, raw.get("user"))
        drive_path = _me_drive(str(raw.get("graphUserId", "")) or None)

        if action == "upload":
            data = action_upload(token, drive_path, raw)
        elif action == "create_folder":
            data = action_create_folder(token, drive_path, raw)
        elif action == "rename":
            data = action_rename(token, drive_path, str(raw.get("id", "")), str(raw.get("name", "")))
        elif action == "move":
            data = action_move(token, drive_path, str(raw.get("id", "")), str(raw.get("destinationId", "")))
        elif action == "copy":
            data = action_copy(token, drive_path, str(raw.get("id", "")), str(raw.get("destinationId", "")), str(raw.get("name", "")))
        elif action == "delete":
            data = action_delete(token, drive_path, str(raw.get("id", "")))
        elif action == "share":
            data = action_share(token, drive_path, str(raw.get("id", "")), raw)
        else:
            raise RuntimeError(f"MISSING_ARG: action no soportada: {action}")

        result = build_success_result(f"graph-files-write ejecutó action={action}", data, settings)
        result["artifactPath"] = write_result_artifact("graph-files-write", action, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        result = build_error_result(message, error_type_from_message(message), settings)
        result["artifactPath"] = write_result_artifact("graph-files-write", action, result)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    cli()
