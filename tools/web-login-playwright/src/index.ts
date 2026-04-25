import fs from "fs";
import path from "path";
import { resolveLoginInput } from "./config";
import { executeLogin } from "./login";
import { ensureArtifactsDir, writeJson } from "./evidence";
import { LoginInput, LoginResult } from "./types";

async function main() {
  const inputFile = process.argv[2];

  if (!inputFile) {
    console.error("Debes enviar un archivo JSON de entrada.");
    process.exit(1);
  }

  const raw = fs.readFileSync(path.resolve(inputFile), "utf-8");
  const input = resolveLoginInput(JSON.parse(raw) as Partial<LoginInput>);

  const artifactsDir = ensureArtifactsDir();
  const screenshotPath = path.join(artifactsDir, "login-result.png");
  const resultPath = path.join(artifactsDir, "result.json");

  const loginExecution = await executeLogin(input, screenshotPath);

  const result: LoginResult = {
    success: loginExecution.success,
    message: loginExecution.message,
    screenshotPath,
    resultPath
  };

  writeJson(resultPath, result);

  console.log(JSON.stringify(result, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});