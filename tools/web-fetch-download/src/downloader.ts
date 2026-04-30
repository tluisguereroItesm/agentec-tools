import { chromium, Download } from "playwright";
import path from "path";
import { FetchDownloadInput, FetchDownloadResult, FlowStep } from "./types";
import { ensureArtifactsDir, safeFilename } from "./evidence";

type YouTubeFormat = "watch" | "shorts" | "live" | "embed" | "youtu.be" | "unknown";

function detectYouTubeFormat(inputUrl: string): YouTubeFormat {
  try {
    const parsed = new URL(inputUrl);
    const host = parsed.hostname.toLowerCase();
    const pathname = parsed.pathname.toLowerCase();

    if (host.includes("youtu.be")) return "youtu.be";
    if (!host.includes("youtube.com")) return "unknown";
    if (pathname.startsWith("/watch")) return "watch";
    if (pathname.startsWith("/shorts/")) return "shorts";
    if (pathname.startsWith("/live")) return "live";
    if (pathname.startsWith("/embed/")) return "embed";
    return "unknown";
  } catch {
    return "unknown";
  }
}

function mapFormatToKind(format: YouTubeFormat): "video" | "short" | "live" | "unknown" {
  switch (format) {
    case "watch":
    case "embed":
    case "youtu.be":
      return "video";
    case "shorts":
      return "short";
    case "live":
      return "live";
    default:
      return "unknown";
  }
}

function resolveYouTubeId(inputUrl: string): string | undefined {
  try {
    const parsed = new URL(inputUrl);
    const host = parsed.hostname.toLowerCase();

    if (host.includes("youtube.com")) {
      const id = parsed.searchParams.get("v") ?? undefined;
      if (id) return id;
      const parts = parsed.pathname.split("/").filter(Boolean);
      const shortsIdx = parts.findIndex((p) => p === "shorts");
      if (shortsIdx >= 0 && parts[shortsIdx + 1]) return parts[shortsIdx + 1];
      return undefined;
    }

    if (host.includes("youtu.be")) {
      const id = parsed.pathname.replace(/^\//, "").trim();
      return id || undefined;
    }
  } catch {
    return undefined;
  }

  return undefined;
}

async function runFlowSteps(
  page: import("playwright").Page,
  steps: FlowStep[] | undefined,
  defaultTimeout: number
): Promise<{ extracted: Record<string, string>; download?: Download }> {
  const extracted: Record<string, string> = {};
  let flowDownload: Download | undefined;

  if (!steps?.length) {
    return { extracted };
  }

  for (const step of steps) {
    const timeout = step.timeoutMs ?? defaultTimeout;

    switch (step.type) {
      case "goto": {
        if (!step.url) throw new Error("Paso goto requiere 'url'.");
        await page.goto(step.url, { waitUntil: "domcontentloaded", timeout });
        break;
      }
      case "fill": {
        if (!step.selector) throw new Error("Paso fill requiere 'selector'.");
        await page.fill(step.selector, step.value ?? "", { timeout });
        break;
      }
      case "click": {
        if (!step.selector) throw new Error("Paso click requiere 'selector'.");
        await page.click(step.selector, { timeout });
        break;
      }
      case "waitForSelector": {
        if (!step.selector) throw new Error("Paso waitForSelector requiere 'selector'.");
        await page.waitForSelector(step.selector, { timeout });
        break;
      }
      case "waitForTimeout": {
        await page.waitForTimeout(Math.max(0, timeout));
        break;
      }
      case "downloadClick": {
        if (!step.selector) throw new Error("Paso downloadClick requiere 'selector'.");
        const [dl] = await Promise.all([
          page.waitForEvent("download", { timeout }),
          page.click(step.selector, { timeout }),
        ]);
        flowDownload = dl;
        break;
      }
      case "extractAttribute": {
        if (!step.selector) throw new Error("Paso extractAttribute requiere 'selector'.");
        if (!step.attribute) throw new Error("Paso extractAttribute requiere 'attribute'.");
        if (!step.key) throw new Error("Paso extractAttribute requiere 'key'.");
        const value = await page.getAttribute(step.selector, step.attribute, { timeout });
        extracted[step.key] = value ?? "";
        break;
      }
      case "extractText": {
        if (!step.selector) throw new Error("Paso extractText requiere 'selector'.");
        if (!step.key) throw new Error("Paso extractText requiere 'key'.");
        const text = (await page.textContent(step.selector, { timeout })) ?? "";
        extracted[step.key] = text.trim();
        break;
      }
      default:
        throw new Error(`Paso no soportado: ${(step as FlowStep).type}`);
    }
  }

  return { extracted, download: flowDownload };
}

export async function fetchAndDownload(
  input: FetchDownloadInput,
  screenshotPath: string
): Promise<FetchDownloadResult> {
  const action = input.action ?? "download-document";
  const timeout = input.timeoutMs ?? 30000;
  const headless = input.headless ?? true;
  const artifactsDir = ensureArtifactsDir();

  const browser = await chromium.launch({ headless });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);

  try {
    await page.goto(input.url, { waitUntil: "domcontentloaded" });

    const { extracted, download: flowDownload } = await runFlowSteps(page, input.steps, timeout);

    if (action === "extract-youtube-id") {
      const extractedId = extracted.youtubeId;
      const urlId = resolveYouTubeId(page.url()) ?? resolveYouTubeId(input.url);
      const videoId = extractedId || urlId;
      const inputFormat = detectYouTubeFormat(input.url);
      const resolvedFormat = detectYouTubeFormat(page.url());
      const format = resolvedFormat !== "unknown" ? resolvedFormat : inputFormat;
      const kind = mapFormatToKind(format);

      await page.screenshot({ path: screenshotPath, fullPage: true });
      await browser.close();

      if (!videoId) {
        return {
          success: false,
          message: "No se pudo extraer el videoId de YouTube. Proporciona una URL watch/shorts/youtu.be válida o un paso extractAttribute.",
          screenshotPath,
          extracted,
          errorType: "YOUTUBE_ID_NOT_FOUND",
        };
      }

      return {
        success: true,
        message: `ID de YouTube extraído correctamente (formato: ${format}).`,
        extracted,
        youtube: {
          videoId,
          format,
          inputFormat,
          resolvedFormat,
          kind,
          canonicalUrl: `https://www.youtube.com/watch?v=${videoId}`,
          watchUrl: page.url(),
        },
        screenshotPath,
      };
    }

    let download: Download | undefined = flowDownload;

    if (!download && input.downloadSelector) {
      const [dl] = await Promise.all([
        page.waitForEvent("download", { timeout }),
        page.click(input.downloadSelector, { timeout }),
      ]);
      download = dl;
    } else if (!download && input.waitForDownload !== false) {
      const [dl] = await Promise.all([
        page.waitForEvent("download", { timeout }),
        page.goto(input.url),
      ]);
      download = dl;
    }

    if (!download) {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      await browser.close();
      return {
        success: false,
        message: "No se detectó descarga. Proporciona un paso downloadClick, downloadSelector o activa waitForDownload.",
        screenshotPath,
        extracted,
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
      extracted,
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
