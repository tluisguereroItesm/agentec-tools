# web-login-playwright

Tool de automatización web para validar login en un portal.

## Alcance v1
- abrir navegador
- navegar a una URL
- capturar usuario y contraseña
- enviar formulario
- validar si el login fue exitoso
- guardar evidencia

## Ejecución local
```bash
npm install
npx playwright install
npm run build
node dist/index.js input.json

# Wrapper temporal de compatibilidad (legacy Node -> backend Python)
npm run start:py-wrapper

# (Opcional) override de entrypoint Python
AGENTEC_WEB_LOGIN_PY_ENTRYPOINT=../web-login-playwright-py/src/main.py npm run start:py-wrapper
```