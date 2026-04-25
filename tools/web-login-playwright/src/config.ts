import fs from "fs";
import path from "path";
import { LoginInput } from "./types";

type LoginProfile = Partial<Omit<LoginInput, "username" | "password">>;

interface LoginProfilesDocument {
  defaultProfile?: string;
  profiles?: Record<string, LoginProfile>;
}

function candidateFiles(explicitConfigFile?: string): string[] {
  const files: string[] = [];

  if (explicitConfigFile) {
    files.push(path.resolve(explicitConfigFile));
  }

  if (process.env.AGENTEC_WEB_LOGIN_CONFIG_FILE) {
    files.push(path.resolve(process.env.AGENTEC_WEB_LOGIN_CONFIG_FILE));
  }

  if (process.env.AGENTEC_STACK_CONFIG_DIR) {
    files.push(path.resolve(process.env.AGENTEC_STACK_CONFIG_DIR, "tools", "web-login", "profiles.json"));
  }

  return [...new Set(files)];
}

function loadDocument(explicitConfigFile?: string): LoginProfilesDocument {
  for (const candidate of candidateFiles(explicitConfigFile)) {
    if (fs.existsSync(candidate)) {
      return JSON.parse(fs.readFileSync(candidate, "utf-8")) as LoginProfilesDocument;
    }
  }

  return { profiles: {} };
}

export function resolveLoginInput(raw: Partial<LoginInput>): LoginInput {
  const doc = loadDocument(raw.configFile);
  const profileName = raw.configProfile ?? process.env.AGENTEC_WEB_LOGIN_PROFILE ?? doc.defaultProfile;
  const profile = profileName ? (doc.profiles?.[profileName] ?? {}) : {};
  const merged = { ...profile, ...raw };

  const missing = ["username", "password", "url", "usernameSelector", "passwordSelector", "submitSelector"].filter(
    (key) => !merged[key as keyof typeof merged]
  );

  if (missing.length > 0) {
    throw new Error(`MISSING_ARG: faltan campos requeridos: ${missing.join(", ")}`);
  }

  return {
    configProfile: profileName,
    configFile: raw.configFile,
    url: merged.url,
    username: merged.username as string,
    password: merged.password as string,
    usernameSelector: merged.usernameSelector,
    passwordSelector: merged.passwordSelector,
    submitSelector: merged.submitSelector,
    successIndicator: merged.successIndicator,
    headless: merged.headless ?? true,
    timeoutMs: merged.timeoutMs ?? 30000,
  };
}