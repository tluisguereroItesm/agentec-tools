#!/usr/bin/env node
/**
 * Wrapper temporal (Paso 10): mantiene entrada legacy Node y redirige a implementación Python.
 * Uso: node scripts/python-wrapper.js input.json
 */

const { spawnSync } = require('node:child_process');
const path = require('node:path');

const inputFile = process.argv[2];
if (!inputFile) {
  console.error('Debes enviar un archivo JSON de entrada.');
  process.exit(1);
}

const defaultPyEntrypoint = path.resolve(__dirname, '../../web-login-playwright-py/src/main.py');
const pyEntrypoint = process.env.AGENTEC_WEB_LOGIN_PY_ENTRYPOINT || defaultPyEntrypoint;

const result = spawnSync('python3', [pyEntrypoint, path.resolve(inputFile)], {
  stdio: 'inherit',
  env: process.env,
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 1);
