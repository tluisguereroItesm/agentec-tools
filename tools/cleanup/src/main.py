from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ─── Safe roots ─────────────────────────────────────────────────────────────
# El tool SOLO puede borrar dentro de estos directorios para evitar path traversal.
# Se resuelven desde el CWD del proceso (dentro del container: /app)
def _safe_roots() -> dict[str, Path]:
    cwd = Path.cwd().resolve()
    roots: dict[str, Path] = {
        "artifacts": (cwd / "artifacts").resolve(),
        "logs":      (cwd / "logs").resolve(),
    }
    # graph-tokens: montado como /app/graph-tokens dentro del container
    tokens_env = os.environ.get("AGENTEC_GRAPH_TOKENS_MOUNT", str(cwd / "graph-tokens"))
    roots["tokens"] = Path(tokens_env).resolve()
    return roots


def _size_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _list_files(directory: Path, pattern: str = "*", older_than_days: int = 0) -> list[Path]:
    if not directory.is_dir():
        return []
    cutoff = time.time() - older_than_days * 86400
    return [
        f for f in sorted(directory.glob(pattern))
        if f.is_file() and (older_than_days == 0 or f.stat().st_mtime < cutoff)
    ]


def action_status(args: dict) -> dict:
    """Muestra uso de disco de artifacts, logs y tokens. No borra nada."""
    roots = _safe_roots()
    report: list[dict] = []
    for label, root in roots.items():
        if not root.is_dir():
            report.append({"dir": label, "path": str(root), "exists": False})
            continue
        files = list(root.rglob("*"))
        files = [f for f in files if f.is_file()]
        total_bytes = sum(f.stat().st_size for f in files)
        # Age breakdown
        now = time.time()
        old_7  = [f for f in files if now - f.stat().st_mtime > 7  * 86400]
        old_30 = [f for f in files if now - f.stat().st_mtime > 30 * 86400]
        report.append({
            "dir":          label,
            "path":         str(root),
            "exists":       True,
            "file_count":   len(files),
            "total_size":   _size_human(total_bytes),
            "older_7_days":  len(old_7),
            "older_30_days": len(old_30),
        })
    return {"status": "ok", "directories": report}


def action_artifacts(args: dict) -> dict:
    """Lista o borra artifacts según antigüedad."""
    dry_run        = bool(args.get("dry_run", True))
    older_than_days = int(args.get("older_than_days", 7))
    pattern_filter = str(args.get("pattern", "*"))

    roots = _safe_roots()
    artifact_dir = roots["artifacts"]

    # Validar patrón para evitar directory traversal
    if ".." in pattern_filter or "/" in pattern_filter or "\\" in pattern_filter:
        return {"status": "error", "message": "Patrón inválido — no se permiten rutas relativas"}

    files = _list_files(artifact_dir, pattern_filter, older_than_days)
    if not files:
        return {
            "status": "ok",
            "message": f"Sin artifacts que limpiar (>{older_than_days} días, patrón: {pattern_filter})",
            "files_deleted": 0,
            "bytes_freed":   "0 B",
            "dry_run":       dry_run,
        }

    deleted: list[str] = []
    bytes_freed = 0
    for f in files:
        size = f.stat().st_size
        if not dry_run:
            f.unlink(missing_ok=True)
        deleted.append(f.name)
        bytes_freed += size

    return {
        "status":        "ok" if not dry_run else "dry_run",
        "action":        "artifacts",
        "older_than_days": older_than_days,
        "pattern":       pattern_filter,
        "dry_run":       dry_run,
        "files_deleted" if not dry_run else "files_to_delete": len(deleted),
        "bytes_freed":   _size_human(bytes_freed),
        "files":         deleted,
    }


def action_logs(args: dict) -> dict:
    """Lista o borra logs según antigüedad."""
    dry_run         = bool(args.get("dry_run", True))
    older_than_days = int(args.get("older_than_days", 30))

    roots    = _safe_roots()
    log_dir  = roots["logs"]

    files = _list_files(log_dir, "*.log", older_than_days) + _list_files(log_dir, "*.log.*", older_than_days)
    # Dedup
    files = list({f.resolve(): f for f in files}.values())

    if not files:
        return {
            "status":  "ok",
            "message": f"Sin logs que limpiar (>{older_than_days} días)",
            "files_deleted": 0,
            "bytes_freed":   "0 B",
            "dry_run":       dry_run,
        }

    deleted: list[str] = []
    bytes_freed = 0
    for f in files:
        size = f.stat().st_size
        if not dry_run:
            f.unlink(missing_ok=True)
        deleted.append(f.name)
        bytes_freed += size

    return {
        "status":   "ok" if not dry_run else "dry_run",
        "action":   "logs",
        "older_than_days": older_than_days,
        "dry_run":  dry_run,
        "files_deleted" if not dry_run else "files_to_delete": len(deleted),
        "bytes_freed": _size_human(bytes_freed),
        "files":    deleted,
    }


def action_purge(args: dict) -> dict:
    """Borra archivos específicos por nombre exacto (sin patrones glob)."""
    dry_run    = bool(args.get("dry_run", True))
    filenames  = args.get("filenames", [])
    target_dir = str(args.get("target_dir", "artifacts"))  # "artifacts" | "logs" | "tokens"

    if not filenames or not isinstance(filenames, list):
        return {"status": "error", "message": "Se requiere 'filenames' como lista de nombres de archivo"}

    roots = _safe_roots()
    if target_dir not in roots:
        return {"status": "error", "message": f"target_dir inválido: '{target_dir}'. Valores: artifacts, logs, tokens"}

    base = roots[target_dir]
    deleted: list[str] = []
    not_found: list[str] = []
    bytes_freed = 0

    for name in filenames:
        # Prevenir path traversal
        if ".." in name or "/" in name or "\\" in name:
            return {"status": "error", "message": f"Nombre de archivo inválido: '{name}'"}
        fpath = (base / name).resolve()
        # Asegurar que esté dentro de la raíz permitida
        if not str(fpath).startswith(str(base)):
            return {"status": "error", "message": f"Ruta no permitida: '{name}'"}
        if not fpath.exists():
            not_found.append(name)
            continue
        size = fpath.stat().st_size
        if not dry_run:
            fpath.unlink(missing_ok=True)
        deleted.append(name)
        bytes_freed += size

    return {
        "status":    "ok" if not dry_run else "dry_run",
        "action":    "purge",
        "target_dir": target_dir,
        "dry_run":   dry_run,
        "files_deleted" if not dry_run else "files_to_delete": len(deleted),
        "bytes_freed": _size_human(bytes_freed),
        "files":     deleted,
        "not_found": not_found,
    }


# ─── Action aliases ──────────────────────────────────────────────────────────
ACTION_ALIASES: dict[str, str] = {
    "status":    "status",
    "info":      "status",
    "disk":      "status",
    "check":     "status",
    "cleanup":   "artifacts",
    "clean":     "artifacts",
    "artifacts": "artifacts",
    "logs":      "logs",
    "log":       "logs",
    "purge":     "purge",
    "delete":    "purge",
    "remove":    "purge",
}

HANDLERS = {
    "status":    action_status,
    "artifacts": action_artifacts,
    "logs":      action_logs,
    "purge":     action_purge,
}

# ─── Entrypoint ──────────────────────────────────────────────────────────────
def main() -> None:
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else "{}"
    try:
        input_data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": f"JSON inválido: {e}"}))
        sys.exit(1)

    action_raw = str(input_data.get("action", "status")).lower().strip()
    action = ACTION_ALIASES.get(action_raw)
    if action is None:
        available = sorted(ACTION_ALIASES.keys())
        print(json.dumps({
            "status":  "error",
            "message": f"Acción '{action_raw}' no reconocida. Disponibles: {available}",
        }))
        sys.exit(1)

    handler = HANDLERS[action]
    result = handler(input_data)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
