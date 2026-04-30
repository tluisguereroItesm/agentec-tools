# web-fetch-download

Tool reusable para flujos web multistep con Playwright (login + navegación + descarga) y extracción de datos web.

## Qué hace
- Navega a cualquier URL con un browser headless (Chromium)
- Detecta y descarga el archivo objetivo via evento de descarga del browser o click en selector
- Ejecuta pasos secuenciales (`steps`) para flujos complejos (login y navegación por varias pantallas)
- Puede extraer ID de YouTube (`action: extract-youtube-id`) para automatizaciones posteriores
- Guarda el archivo en `artifacts/`
- Genera screenshot como evidencia

## Cuándo usarla
- El documento está en una URL directa (PDF, DOCX, XLSX, etc.)
- El documento requiere hacer click en un botón/enlace para descargarse
- El acceso requiere login/interacciones en múltiples pasos
- Necesitas extraer un identificador (por ejemplo, `videoId` de YouTube)

## Ejecución local
```bash
npm install
npm run build
node dist/index.js input.example.json
```

## Input de ejemplo
```json
{
  "action": "download-document",
  "url": "https://www.w3.org/WAI/WCAG21/wcag21.pdf",
  "headless": true,
  "timeoutMs": 30000
}
```

## Con flujo multistep (login + descarga)
```json
{
  "action": "download-document",
  "url": "https://mi-portal.com/login",
  "steps": [
    { "type": "fill", "selector": "#username", "value": "usuario@empresa.com" },
    { "type": "fill", "selector": "#password", "value": "********" },
    { "type": "click", "selector": "button[type='submit']" },
    { "type": "waitForSelector", "selector": "a#download-report" },
    { "type": "downloadClick", "selector": "a#download-report" }
  ],
  "headless": true
}
```

## Extraer ID de YouTube (sin descargar video)
```json
{
  "action": "extract-youtube-id",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
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
  "extracted": {},
  "screenshotPath": "/app/artifacts/fetch-screenshot-1234567890.png",
  "resultPath": "/app/artifacts/fetch-result-1234567890.json"
}
```

Para `extract-youtube-id`, la respuesta incluye:

```json
{
  "success": true,
  "youtube": {
    "videoId": "dQw4w9WgXcQ",
    "canonicalUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "watchUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  }
}
```

## Notas
- Respeta términos de uso de cada sitio web y los derechos de autor.
- Esta tool no implementa descarga de videos de plataformas protegidas.
- Los archivos descargados se almacenan en `artifacts/` del contenedor.
