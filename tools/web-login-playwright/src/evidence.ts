import fs from "fs";
import path from "path";

export function ensureArtifactsDir(): string {
  const dir = path.resolve(process.cwd(), "artifacts");
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  return dir;
}

export function writeJson(filePath: string, data: unknown): void {
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf-8");
}