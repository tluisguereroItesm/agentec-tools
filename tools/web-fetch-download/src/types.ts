export type FetchAction = "download-document" | "extract-youtube-id";

export type FlowStepType =
  | "goto"
  | "fill"
  | "click"
  | "waitForSelector"
  | "waitForTimeout"
  | "downloadClick"
  | "extractAttribute"
  | "extractText";

export interface FlowStep {
  type: FlowStepType;
  url?: string;
  selector?: string;
  value?: string;
  attribute?: string;
  key?: string;
  timeoutMs?: number;
}

export interface FetchDownloadInput {
  action?: FetchAction;
  url: string;
  configProfile?: string;
  configFile?: string;
  username?: string;
  password?: string;
  downloadSelector?: string;
  waitForDownload?: boolean;
  steps?: FlowStep[];
  headless?: boolean;
  timeoutMs?: number;
}

export interface FetchDownloadResult {
  success: boolean;
  message: string;
  filePath?: string;
  fileName?: string;
  mimeType?: string;
  extracted?: Record<string, string>;
  youtube?: {
    videoId?: string;
    format?: "watch" | "shorts" | "live" | "embed" | "youtu.be" | "unknown";
    inputFormat?: "watch" | "shorts" | "live" | "embed" | "youtu.be" | "unknown";
    resolvedFormat?: "watch" | "shorts" | "live" | "embed" | "youtu.be" | "unknown";
    kind?: "video" | "short" | "live" | "unknown";
    canonicalUrl?: string;
    watchUrl?: string;
  };
  screenshotPath?: string;
  resultPath?: string;
  errorType?: string;
}
