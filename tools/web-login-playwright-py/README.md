# web-login-playwright-py

Implementación Python-first de la tool `web_login_playwright`.

## Qué hace
- abre navegador Chromium
- navega a URL de login
- llena credenciales
- envía formulario
- valida éxito opcional
- genera screenshot y resultado JSON en `artifacts/`

## Ejecución local
1. Instalar dependencias de Python del proyecto.
2. Instalar navegadores Playwright (`playwright install chromium`).
3. Ejecutar:
   - `python src/main.py input.example.json`

## Salida
Imprime un JSON con:
- `success`
- `message`
- `screenshotPath`
- `resultPath`
- `backend` (`python-playwright`)
