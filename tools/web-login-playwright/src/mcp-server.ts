import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import http from "node:http";
import path from "path";
import { executeLogin } from "./login";
import { ensureArtifactsDir, writeJson } from "./evidence";

const server = new Server(
  { name: "web-login-playwright", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "web_login_playwright",
      description:
        "Executes a browser-based login flow using Playwright and generates screenshot evidence. Use for login monitoring, smoke tests, and authentication validation.",
      inputSchema: {
        type: "object",
        required: ["url", "username", "password", "usernameSelector", "passwordSelector", "submitSelector"],
        properties: {
          url:               { type: "string",  description: "Login page URL" },
          username:          { type: "string",  description: "Username" },
          password:          { type: "string",  description: "Password" },
          usernameSelector:  { type: "string",  description: "CSS selector for username field" },
          passwordSelector:  { type: "string",  description: "CSS selector for password field" },
          submitSelector:    { type: "string",  description: "CSS selector for submit button" },
          successIndicator:  { type: "string",  description: "CSS selector that confirms a successful login" },
          headless:          { type: "boolean", description: "Run browser headless (default: true)" },
          timeoutMs:         { type: "integer", description: "Timeout in milliseconds (default: 30000)" },
        },
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "web_login_playwright") {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }

  const input = request.params.arguments as {
    url: string;
    username: string;
    password: string;
    usernameSelector: string;
    passwordSelector: string;
    submitSelector: string;
    successIndicator?: string;
    headless?: boolean;
    timeoutMs?: number;
  };

  const artifactsDir = ensureArtifactsDir();
  const screenshotPath = path.join(artifactsDir, `login-result-${Date.now()}.png`);
  const resultPath = path.join(artifactsDir, `result-${Date.now()}.json`);

  const execution = await executeLogin(input, screenshotPath);

  const result = {
    success: execution.success,
    message: execution.message,
    screenshotPath,
    resultPath,
  };

  writeJson(resultPath, result);

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(result, null, 2),
      },
    ],
    isError: !execution.success,
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

  // HTTP / Streamable-HTTP mode (default)
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
      res.end(JSON.stringify({ status: "ok", server: "web-login-playwright-mcp" }));
    } else {
      res.writeHead(404);
      res.end();
    }
  });

  httpServer.listen(port, "0.0.0.0", () => {
    console.log(`web-login-playwright MCP server listening on http://0.0.0.0:${port}/mcp`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
