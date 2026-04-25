# doc-reader

Tool reusable para extraer texto de documentos locales: PDF, DOCX, XLSX, TXT y Markdown.

## Qué hace
- Detecta el tipo de archivo por extensión
- Extrae texto estructurado con librerías especializadas
- Devuelve contenido, conteo de páginas y palabras
- Limita la extracción con `maxChars` para archivos grandes
- Guarda el resultado como artifact en `artifacts/`

## Formatos soportados
| Extensión | Librería |
|-----------|----------|
| `.pdf` | `pdfplumber` |
| `.docx` | `python-docx` |
| `.xlsx` | `openpyxl` |
| `.txt` | built-in |
| `.md` | built-in |
| `.csv` | built-in |

## Ejecución local
```bash
pip install -e .
python src/main.py input.example.json
```

## Input de ejemplo
```json
{
  "filePath": "/app/artifacts/documento.pdf",
  "maxChars": 8000,
  "includeMetadata": true
}
```

## Output de ejemplo
```json
{
  "success": true,
  "message": "Texto extraído de documento.pdf",
  "content": "...",
  "pageCount": 5,
  "wordCount": 1240,
  "charCount": 7800,
  "fileType": "pdf",
  "truncated": false,
  "extractedAt": "2026-04-23T15:00:00+00:00",
  "artifactPath": "/app/artifacts/doc-reader-20260423T150000.json"
}
```

## Notas
- v1 es solo lectura — no modifica ni mueve archivos.
- Para archivos protegidos con contraseña, devuelve `EXTRACTION_ERROR`.
- Encadenar con `doc-summarize` skill para obtener resumen del contenido.
