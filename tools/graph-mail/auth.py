from __future__ import annotations

import os
import sys
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
from graph_runtime import run_auth_cli


if __name__ == "__main__":
    run_auth_cli("mail")
