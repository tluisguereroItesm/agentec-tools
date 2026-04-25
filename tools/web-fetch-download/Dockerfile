FROM mcr.microsoft.com/playwright:v1.59.1-noble

WORKDIR /app

COPY package*.json tsconfig.json ./
RUN npm ci

RUN npx playwright install chromium

COPY src ./src
RUN npm run build

RUN mkdir -p /app/artifacts

ENV MCP_TRANSPORT=http
ENV MCP_PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
  CMD wget -qO- http://localhost:3000/health || exit 1

CMD ["node", "dist/mcp-server.js"]
