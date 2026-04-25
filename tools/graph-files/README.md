# graph-files

Tool reusable para inspección de archivos OneDrive/SharePoint vía Microsoft Graph.

## Qué hace
- listar recientes
- buscar archivos
- leer y extraer contenido
- generar resumen sencillo

## Configuración
La tool resuelve su configuración desde:
1. `profile` en el input
2. variables `AGENTEC_GRAPH_*`
3. `config/tools/graph/profiles.json`

## Autenticación local
Comparte la misma autenticación reusable de Graph.

Ejemplos:
- `python auth.py login --profile default`
- `python auth.py status --profile default`

## Ejecución local
```bash
python src/main.py input.example.json
```

## Notas
- v1 no elimina ni modifica archivos.
- Usa `driveMode=me` por default.
- Puede apuntar a un sitio SharePoint con `siteHostname` y `sitePath`.
