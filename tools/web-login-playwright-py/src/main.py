from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright


@dataclass
class LoginInput:
    url: str
    username: str
    password: str
    usernameSelector: str
    passwordSelector: str
    submitSelector: str
    successIndicator: str | None = None
    headless: bool = True
    timeoutMs: int = 30000


def _load_profiles(config_file: str | None = None) -> dict:
    explicit = Path(config_file).expanduser() if config_file else None
    stack_config = os.environ.get("AGENTEC_STACK_CONFIG_DIR", "")
    env_file = os.environ.get("AGENTEC_WEB_LOGIN_CONFIG_FILE", "")
    candidates = [c for c in [explicit, Path(env_file).expanduser() if env_file else None, Path(stack_config).expanduser() / "tools" / "web-login" / "profiles.json" if stack_config else None] if c]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {"profiles": {}}


def _read_input(path_arg: str) -> LoginInput:
    raw = json.loads(Path(path_arg).read_text(encoding="utf-8"))
    profiles_doc = _load_profiles(raw.get("configFile"))
    profile_name = raw.get("configProfile") or os.environ.get("AGENTEC_WEB_LOGIN_PROFILE") or profiles_doc.get("defaultProfile")
    profile = (profiles_doc.get("profiles") or {}).get(profile_name, {}) if profile_name else {}
    merged = {**profile, **raw}

    missing = [
        key
        for key in ["username", "password", "url", "usernameSelector", "passwordSelector", "submitSelector"]
        if not merged.get(key)
    ]
    if missing:
        raise ValueError(f"MISSING_ARG: faltan campos requeridos: {', '.join(missing)}")

    return LoginInput(
        url=merged["url"],
        username=merged["username"],
        password=merged["password"],
        usernameSelector=merged["usernameSelector"],
        passwordSelector=merged["passwordSelector"],
        submitSelector=merged["submitSelector"],
        successIndicator=merged.get("successIndicator"),
        headless=bool(merged.get("headless", True)),
        timeoutMs=int(merged.get("timeoutMs", 30000)),
    )


def _ensure_artifacts_dir() -> Path:
    artifacts = Path.cwd() / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


def run_login(input_data: LoginInput, screenshot_path: Path) -> tuple[bool, str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=input_data.headless)
        page = browser.new_page()
        page.set_default_timeout(input_data.timeoutMs)
        try:
            page.goto(input_data.url, wait_until="domcontentloaded")
            page.fill(input_data.usernameSelector, input_data.username)
            page.fill(input_data.passwordSelector, input_data.password)
            page.click(input_data.submitSelector)

            if input_data.successIndicator:
                page.wait_for_selector(input_data.successIndicator, timeout=input_data.timeoutMs)

            page.screenshot(path=str(screenshot_path), full_page=True)
            return True, "Login ejecutado correctamente"
        except Exception as exc:  # noqa: BLE001
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            return False, str(exc)
        finally:
            browser.close()


def cli() -> None:
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not input_file:
        print("Debes enviar un archivo JSON de entrada.", file=sys.stderr)
        sys.exit(1)

    input_data = _read_input(input_file)
    artifacts_dir = _ensure_artifacts_dir()
    screenshot_path = artifacts_dir / "login-result-py.png"
    result_path = artifacts_dir / "result-py.json"

    success, message = run_login(input_data, screenshot_path)

    result = {
        "success": success,
        "message": message,
        "screenshotPath": str(screenshot_path),
        "resultPath": str(result_path),
        "backend": "python-playwright",
    }

    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    cli()
