FROM mcr.microsoft.com/playwright:v1.59.1-noble

WORKDIR /app

COPY package*.json tsconfig.json ./
RUN npm ci

# Pre-download Chromium (already bundled in the base image, this ensures browsers are ready)
RUN npx playwright install chromium

COPY src ./src
RUN npm run build

RUN mkdir -p /app/artifacts

# Default: standalone tool runner (one-shot via index.js)
# Override with MCP_TRANSPORT=http for the persistent MCP HTTP server
ENV MCP_TRANSPORT=http
ENV MCP_PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
  CMD wget -qO- http://localhost:3000/health || exit 1

CMD ["node", "dist/mcp-server.js"]