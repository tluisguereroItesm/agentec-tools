import fs from "fs";
import path from "path";
import { fetchAndDownload } from "./downloader";
import { ensureArtifactsDir, writeJson } from "./evidence";
import { FetchDownloadInput } from "./types";

async function main() {
  const inputFile = process.argv[2];
  if (!inputFile) {
    console.error("Uso: node dist/index.js <input.json>");
    process.exit(1);
  }

  const raw = fs.readFileSync(path.resolve(inputFile), "utf-8");
  const input = JSON.parse(raw) as FetchDownloadInput;

  const artifactsDir = ensureArtifactsDir();
  const ts = Date.now();
  const screenshotPath = path.join(artifactsDir, `fetch-screenshot-${ts}.png`);
  const resultPath = path.join(artifactsDir, `fetch-result-${ts}.json`);

  const result = await fetchAndDownload(input, screenshotPath);
  result.resultPath = resultPath;

  writeJson(resultPath, result);
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.success ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
