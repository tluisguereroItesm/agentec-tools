"""curp-downloader — Descarga el comprobante CURP desde gob.mx/curp.

Entrega el PDF de tres formas (parámetro `delivery`):
  email    → adjunto de correo via Microsoft Graph  (requiere `to`)
  onedrive → sube al OneDrive del usuario autenticado (opcional `remoteFolder`)
  artifact → guarda el archivo localmente y devuelve la ruta (default)

Uso:
  python src/main.py /input.json
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Bootstrap shared path ─────────────────────────────────────────────────────

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

from graph_runtime import (  # noqa: E402
    build_error_result,
    error_type_from_message,
    get_valid_token,
    init_login,
    poll_login,
    resolve_graph_settings,
    write_result_artifact,
)

# ── Constants ─────────────────────────────────────────────────────────────────

CURP_URL = "https://www.gob.mx/curp/"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Selectors — ordered most-specific to least-specific
_CURP_INPUT_SELECTORS = [
    "#datos",
    "input[maxlength='18']",
    "input[id*='curp' i]",
    "input[name*='curp' i]",
    "input[placeholder*='CURP' i]",
    "input[aria-label*='CURP' i]",
    "input[type='text']",
]

_SEARCH_BTN_SELECTORS = [
    "#buttonBuscar",
    "button#buscar",
    "button[onclick*='buscar' i]",
    "button:has-text('Buscar')",
    "input[type='submit'][value='Buscar']",
    "input[type='button'][value='Buscar']",
]

_RESULT_SELECTORS = [
    "#CURP_RESULT",
    ".curp-datos",
    "#formulario_datos",
    "#curpResult",
    "#resultado",
    ".resultado",
    "[id*='result' i]",
    "[class*='resultado']",
    "td:has-text('CURP')",
    "span:has-text('CURP')",
    "p:has-text('CURP')",
    "#curp",
    ".datos",
]

_DOWNLOAD_BTN_SELECTORS = [
    "a:has-text('Descargar CURP')",
    "button:has-text('Descargar CURP')",
    "a:has-text('Descargar')",
    "button:has-text('Descargar')",
    "input[type='button'][value*='Descargar' i]",
    "a[href*='.pdf']",
    "a[href*='curp' i][href*='pdf' i]",
    "[onclick*='download' i]",
    "[onclick*='pdf' i]",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _artifacts_dir() -> Path:
    d = Path(os.environ.get("AGENTEC_ARTIFACTS_DIR", "/app/artifacts"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_input(path_arg: str) -> dict[str, Any]:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


def _build_success(data: dict[str, Any], settings: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "message": "ok",
        "data": data,
        "backend": "playwright+urllib",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if settings:
        result["profile"] = settings.profile_name
        result["tenantId"] = settings.tenant_id
    return result


def _http_download(url: str, dest: Path) -> None:
    """Download a URL to a local file."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())


# ── Playwright CURP download ─────────────────────────────────────────────────

def download_curp_pdf(curp: str, artifacts_dir: Path, timeout_ms: int = 60_000, headless: bool = True) -> tuple[Path, str]:
    """Navigate to gob.mx/curp, fill form, download the PDF.

    Returns (pdf_path, filename).
    Raises RuntimeError with descriptive code prefix on failure.
    """
    from playwright.sync_api import (  # type: ignore[import]
        sync_playwright,
        TimeoutError as PlaywrightTimeout,
    )

    ts = int(time.time() * 1000)
    out_path = artifacts_dir / f"CURP_{curp}_{ts}.pdf"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            # ── Step 1: Navigate ─────────────────────────────────────────────
            page.goto(CURP_URL, wait_until="domcontentloaded", timeout=timeout_ms)

            # ── Step 2: Locate and fill CURP input ───────────────────────────
            input_sel: str | None = None
            for sel in _CURP_INPUT_SELECTORS:
                try:
                    page.wait_for_selector(sel, state="visible", timeout=5_000)
                    input_sel = sel
                    break
                except PlaywrightTimeout:
                    continue

            if not input_sel:
                raise RuntimeError(
                    "CURP_SITE_ERROR: No se encontró el campo de entrada CURP. "
                    "El sitio puede haber cambiado — revisar los selectores."
                )

            page.fill(input_sel, curp)

            # ── Step 3: Click Buscar ─────────────────────────────────────────
            clicked_search = False
            for sel in _SEARCH_BTN_SELECTORS:
                try:
                    page.click(sel, timeout=5_000)
                    clicked_search = True
                    break
                except Exception:  # noqa: BLE001
                    continue

            if not clicked_search:
                raise RuntimeError(
                    "CURP_SITE_ERROR: No se encontró el botón 'Buscar'. "
                    "El sitio puede haber cambiado."
                )

            # ── Step 4: Wait for results ─────────────────────────────────────
            # Some results appear in network requests; wait for any result indicator
            combined = ", ".join(_RESULT_SELECTORS)
            try:
                page.wait_for_selector(combined, timeout=timeout_ms)
            except PlaywrightTimeout:
                # Check for "no encontrado" messages before giving up
                page_text = page.inner_text("body").lower()
                if any(w in page_text for w in ["no existe", "no encontrado", "no se encontró", "curp inválido"]):
                    raise RuntimeError(f"CURP_NOT_FOUND: El CURP '{curp}' no existe o es inválido.")
                raise RuntimeError(
                    "CURP_SITE_ERROR: Timeout esperando resultado. El sitio puede estar lento o el CURP no existe."
                )

            # ── Step 5: Download PDF ─────────────────────────────────────────
            downloaded = False

            for sel in _DOWNLOAD_BTN_SELECTORS:
                try:
                    page.wait_for_selector(sel, state="visible", timeout=5_000)
                except PlaywrightTimeout:
                    continue

                # Try browser download event first
                try:
                    with page.expect_download(timeout=20_000) as dl_info:
                        page.click(sel, timeout=5_000)
                    dl = dl_info.value
                    dl.save_as(str(out_path))
                    downloaded = True
                    break
                except Exception:  # noqa: BLE001
                    pass

                # Fallback: extract href and download directly
                try:
                    href = page.get_attribute(sel, "href")
                    if href and (href.endswith(".pdf") or "pdf" in href.lower()):
                        _http_download(href, out_path)
                        downloaded = True
                        break
                except Exception:  # noqa: BLE001
                    pass

            if not downloaded:
                # Last resort: find any PDF link on the page
                try:
                    hrefs: list[str] = page.eval_on_selector_all(
                        "a[href*='.pdf'], a[href*='PDF']",
                        "els => els.map(e => e.href).filter(h => h)",
                    )
                    for href in hrefs:
                        try:
                            _http_download(href, out_path)
                            downloaded = True
                            break
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    pass

            if not downloaded:
                raise RuntimeError(
                    "CURP_DOWNLOAD_ERROR: No se pudo descargar el PDF. "
                    "Posibles causas: CURP no encontrado, el sitio cambió sus selectores, "
                    "o la descarga requiere interacción manual."
                )

            return out_path, out_path.name

        finally:
            browser.close()


# ── Graph delivery helpers ────────────────────────────────────────────────────

def _graph_request(method: str, path: str, token: str, body: bytes | None = None, content_type: str = "application/json") -> dict[str, Any]:
    """Execute a Microsoft Graph API request."""
    url = f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def send_email_with_attachment(
    token: str,
    to: str,
    subject: str,
    body_html: str,
    filename: str,
    file_bytes: bytes,
) -> dict[str, Any]:
    """Send an email with a PDF attachment via Microsoft Graph /me/sendMail."""
    to_list = [{"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()]
    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": to_list,
        "attachments": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(file_bytes).decode("ascii"),
            }
        ],
    }
    payload = json.dumps({"message": message, "saveToSentItems": True}).encode()
    _graph_request("POST", "/me/sendMail", token, body=payload)
    return {
        "deliveryType": "email",
        "status": "sent",
        "to": to,
        "attachment": filename,
    }


def upload_to_onedrive(
    token: str,
    filename: str,
    file_bytes: bytes,
    remote_folder: str,
) -> dict[str, Any]:
    """Upload a file to OneDrive via Microsoft Graph simple upload."""
    safe_folder = urllib.parse.quote(remote_folder.strip("/"), safe="")
    safe_name = urllib.parse.quote(filename, safe="")
    path = f"/me/drive/root:/{safe_folder}/{safe_name}:/content"
    result = _graph_request("PUT", path, token, body=file_bytes, content_type="application/pdf")
    return {
        "deliveryType": "onedrive",
        "status": "uploaded",
        "webUrl": result.get("webUrl", ""),
        "name": result.get("name", filename),
        "remotePath": f"{remote_folder}/{filename}",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps(build_error_result("MISSING_ARG: se requiere ruta al archivo de input", "MISSING_ARG")))
        sys.exit(1)

    input_data = _load_input(sys.argv[1])
    action = (input_data.get("action") or "download").lower()

    # ── Auth actions (login / poll) ──────────────────────────────────────────
    if action in ("auth-login", "auth-poll"):
        settings = resolve_graph_settings("mail", input_data)
        result = init_login(settings) if action == "auth-login" else poll_login(settings)
        print(json.dumps(result))
        return

    # ── Validate CURP ────────────────────────────────────────────────────────
    curp = (input_data.get("curp") or "").strip().upper()
    if not curp:
        print(json.dumps(build_error_result("MISSING_ARG: falta el parámetro 'curp'", "MISSING_ARG")))
        sys.exit(1)
    if len(curp) != 18:  # noqa: PLR2004
        print(json.dumps(build_error_result(
            f"MISSING_ARG: CURP debe tener exactamente 18 caracteres (recibido: '{curp}' con {len(curp)})",
            "MISSING_ARG",
        )))
        sys.exit(1)

    delivery = (input_data.get("delivery") or "artifact").lower()
    headless = input_data.get("headless", True)
    timeout_ms = int(input_data.get("timeoutMs") or 60_000)

    settings = None
    try:
        artifacts_dir = _artifacts_dir()

        # ── Download CURP PDF ─────────────────────────────────────────────────
        pdf_path, filename = download_curp_pdf(
            curp, artifacts_dir, timeout_ms=timeout_ms, headless=headless
        )
        pdf_bytes = pdf_path.read_bytes()

        # ── Deliver ───────────────────────────────────────────────────────────
        delivery_result: dict[str, Any]

        if delivery == "email":
            to = (input_data.get("to") or "").strip()
            if not to:
                raise RuntimeError("MISSING_ARG: falta 'to' para delivery=email")
            subject = input_data.get("subject") or f"Comprobante CURP: {curp}"
            body_html = (
                input_data.get("body")
                or f"<p>Adjunto encontrarás el comprobante de la CURP <strong>{curp}</strong>.</p>"
                   f"<p><small>Descargado desde <a href='{CURP_URL}'>{CURP_URL}</a></small></p>"
            )
            settings = resolve_graph_settings("mail", input_data)
            token = get_valid_token(settings)
            delivery_result = send_email_with_attachment(token, to, subject, body_html, filename, pdf_bytes)

        elif delivery == "onedrive":
            remote_folder = (input_data.get("remoteFolder") or "CURP").strip("/")
            settings = resolve_graph_settings("mail", input_data)
            token = get_valid_token(settings)
            delivery_result = upload_to_onedrive(token, filename, pdf_bytes, remote_folder)

        else:  # artifact (default)
            delivery_result = {
                "deliveryType": "artifact",
                "status": "saved",
                "pdfPath": str(pdf_path),
            }

        result = _build_success(
            data={
                "curp": curp,
                "pdfPath": str(pdf_path),
                "fileName": filename,
                "fileSizeBytes": len(pdf_bytes),
                "delivery": delivery_result,
            },
            settings=settings,
        )
        artifact_path = write_result_artifact("curp-downloader", "download", result)
        result["artifactPath"] = artifact_path
        print(json.dumps(result, ensure_ascii=False))

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        error_type = error_type_from_message(msg)
        print(json.dumps(build_error_result(msg, error_type, settings)))
        sys.exit(1)


if __name__ == "__main__":
    main()
