import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import http from "node:http";
import path from "path";
import { fetchAndDownload } from "./downloader";
import { ensureArtifactsDir, writeJson } from "./evidence";
import { FetchDownloadInput } from "./types";

const server = new Server(
  { name: "web-fetch-download", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "web_fetch_download",
      description:
        "Navigates to a URL with Playwright and downloads the target document. Optionally executes a login step first using a configured profile. Returns the local file path and a screenshot as evidence.",
      inputSchema: {
        type: "object",
        required: ["url"],
        properties: {
          url:              { type: "string",  description: "URL of the page or direct download link" },
          configProfile:    { type: "string",  description: "Login profile name from profiles.json (if auth is needed)" },
          configFile:       { type: "string",  description: "Path to custom profiles file" },
          username:         { type: "string",  description: "Username for login (overrides profile)" },
          password:         { type: "string",  description: "Password for login (overrides profile)" },
          downloadSelector: { type: "string",  description: "CSS selector of the element to click to trigger download" },
          waitForDownload:  { type: "boolean", description: "Wait for a browser download event (default: true)" },
          headless:         { type: "boolean", description: "Run browser headless (default: true)" },
          timeoutMs:        { type: "integer", description: "Timeout in milliseconds (default: 30000)" },
        },
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "web_fetch_download") {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }

  const input = request.params.arguments as unknown as FetchDownloadInput;
  const artifactsDir = ensureArtifactsDir();
  const ts = Date.now();
  const screenshotPath = path.join(artifactsDir, `fetch-screenshot-${ts}.png`);
  const resultPath = path.join(artifactsDir, `fetch-result-${ts}.json`);

  const result = await fetchAndDownload(input, screenshotPath);
  result.resultPath = resultPath;
  writeJson(resultPath, result);

  return {
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    isError: !result.success,
  };
});

async function main() {
  const mode = process.env.MCP_TRANSPORT ?? "http";
  const port = parseInt(process.env.MCP_PORT ?? "3000", 10);

  if (mode === "stdio") {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    return;
  }

  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  await server.connect(transport);

  const httpServer = http.createServer((req, res) => {
    if (req.url === "/mcp" || req.url === "/mcp/") {
      let body = "";
      req.on("data", (chunk: Buffer) => { body += chunk.toString(); });
      req.on("end", () => {
        const parsed = body ? JSON.parse(body) : undefined;
        transport.handleRequest(req, res, parsed);
      });
    } else if (req.url === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok", server: "web-fetch-download" }));
    } else {
      res.writeHead(404);
      res.end();
    }
  });

  httpServer.listen(port, "0.0.0.0", () => {
    console.log(`web-fetch-download MCP server listening on http://0.0.0.0:${port}/mcp`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
