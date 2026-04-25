# web-fetch-download

Tool reusable para descargar documentos desde páginas web usando Playwright.

## Qué hace
- Navega a cualquier URL con un browser headless (Chromium)
- Opcionalmente ejecuta login previo usando un perfil configurado
- Detecta y descarga el archivo objetivo via evento de descarga del browser o click en selector
- Guarda el archivo en `artifacts/`
- Genera screenshot como evidencia

## Cuándo usarla
- El documento está en una URL directa (PDF, DOCX, XLSX, etc.)
- El documento requiere hacer click en un botón/enlace para descargarse
- El acceso requiere login previo (usar `configProfile`)

## Ejecución local
```bash
npm install
npm run build
node dist/index.js input.example.json
```

## Input de ejemplo
```json
{
  "url": "https://www.w3.org/WAI/WCAG21/wcag21.pdf",
  "headless": true,
  "timeoutMs": 30000
}
```

## Con login previo
```json
{
  "url": "https://mi-portal.com/documentos/reporte.pdf",
  "configProfile": "mi-sistema",
  "downloadSelector": "a.download-btn",
  "headless": true
}
```

## Output
```json
{
  "success": true,
  "message": "Archivo descargado correctamente: reporte.pdf",
  "filePath": "/app/artifacts/reporte.pdf",
  "fileName": "reporte.pdf",
  "screenshotPath": "/app/artifacts/fetch-screenshot-1234567890.png",
  "resultPath": "/app/artifacts/fetch-result-1234567890.json"
}
```

## Notas
- v1 no navega páginas complejas con flujos de múltiples pasos post-login.
- Para flujos de login complejos, usar `web-login-playwright` primero.
- Los archivos descargados se almacenan en `artifacts/` del contenedor.
