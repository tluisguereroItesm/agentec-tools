FROM mcr.microsoft.com/playwright:v1.52.0-jammy

WORKDIR /app

COPY package.json tsconfig.json ./
RUN npm install

COPY src ./src
RUN npm run build

RUN mkdir -p /app/artifacts

ENTRYPOINT ["node", "dist/index.js"]