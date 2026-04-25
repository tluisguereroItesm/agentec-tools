export interface FetchDownloadInput {
  url: string;
  configProfile?: string;
  configFile?: string;
  username?: string;
  password?: string;
  downloadSelector?: string;
  waitForDownload?: boolean;
  headless?: boolean;
  timeoutMs?: number;
}

export interface FetchDownloadResult {
  success: boolean;
  message: string;
  filePath?: string;
  fileName?: string;
  mimeType?: string;
  screenshotPath?: string;
  resultPath?: string;
  errorType?: string;
}
