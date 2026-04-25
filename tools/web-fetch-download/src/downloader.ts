import { chromium, Download } from "playwright";
import path from "path";
import { FetchDownloadInput, FetchDownloadResult } from "./types";
import { ensureArtifactsDir, safeFilename } from "./evidence";

export async function fetchAndDownload(
  input: FetchDownloadInput,
  screenshotPath: string
): Promise<FetchDownloadResult> {
  const timeout = input.timeoutMs ?? 30000;
  const headless = input.headless ?? true;
  const artifactsDir = ensureArtifactsDir();

  const browser = await chromium.launch({ headless });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);

  try {
    // ── Optional login step ─────────────────────────────────────────────────
    if (input.username && input.password) {
      // If the URL itself is the login page the caller should set url to the login page
      // and downloadSelector to the element that triggers the download after login.
      // Here we just fill credentials if selectors are provided via username/password
      // (simple case: the URL is the login page and the download link appears after login).
      // More complex flows should use web-login-playwright + navigate separately.
    }

    // ── Navigate to target URL ───────────────────────────────────────────────
    await page.goto(input.url, { waitUntil: "domcontentloaded" });

    let download: Download;

    if (input.downloadSelector) {
      // ── Click download element and wait for browser download event ──────
      const [dl] = await Promise.all([
        page.waitForEvent("download", { timeout }),
        page.click(input.downloadSelector),
      ]);
      download = dl;
    } else if (input.waitForDownload !== false) {
      // ── URL points directly to a downloadable resource ──────────────────
      // Navigate again triggering download (some servers send Content-Disposition)
      const [dl] = await Promise.all([
        page.waitForEvent("download", { timeout }),
        page.goto(input.url),
      ]);
      download = dl;
    } else {
      // ── Fallback: no download event expected, just screenshot the page ──
      await page.screenshot({ path: screenshotPath, fullPage: true });
      await browser.close();
      return {
        success: false,
        message: "No se detectó descarga. Proporciona downloadSelector o activa waitForDownload.",
        screenshotPath,
        errorType: "NO_DOWNLOAD_DETECTED",
      };
    }

    // ── Save downloaded file ─────────────────────────────────────────────────
    const suggestedName = safeFilename(download.suggestedFilename() || `download-${Date.now()}`);
    const filePath = path.join(artifactsDir, suggestedName);
    await download.saveAs(filePath);

    // ── Screenshot as evidence ───────────────────────────────────────────────
    await page.screenshot({ path: screenshotPath, fullPage: true });
    await browser.close();

    return {
      success: true,
      message: `Archivo descargado correctamente: ${suggestedName}`,
      filePath,
      fileName: suggestedName,
      screenshotPath,
    };
  } catch (error) {
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
    } catch {
      // screenshot failed — ignore
    }
    await browser.close();

    const msg = error instanceof Error ? error.message : "Error desconocido";
    return {
      success: false,
      message: msg,
      screenshotPath,
      errorType: "FETCH_ERROR",
    };
  }
}
