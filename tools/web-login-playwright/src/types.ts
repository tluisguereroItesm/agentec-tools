export interface LoginInput {
  configProfile?: string;
  configFile?: string;
  url?: string;
  username: string;
  password: string;
  usernameSelector?: string;
  passwordSelector?: string;
  submitSelector?: string;
  successIndicator?: string;
  headless?: boolean;
  timeoutMs?: number;
}

export interface LoginResult {
  success: boolean;
  message: string;
  screenshotPath: string;
  resultPath: string;
}