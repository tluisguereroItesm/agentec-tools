from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from graph_runtime import run_auth_cli


if __name__ == "__main__":
    run_auth_cli("files")
