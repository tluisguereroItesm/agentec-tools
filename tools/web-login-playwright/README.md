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