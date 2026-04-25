# graph-mail

Tool reusable para correo Microsoft 365 vía Microsoft Graph.

## Qué hace
- leer correos sin leer
- generar digest ejecutivo
- buscar correos por tema
- leer correo completo
- extraer tareas
- detectar correos sin respuesta
- generar radar por proyecto
- sugerir borradores de respuesta

## Configuración
La tool resuelve su configuración desde:
1. `profile` en el input
2. variables de entorno del stack (`AGENTEC_GRAPH_*`)
3. `config/tools/graph/profiles.json`

## Autenticación local
Usa device code flow con el helper `auth.py`.

Ejemplos:
- `python auth.py login --profile default`
- `python auth.py status --profile default`
- `python auth.py list --profile default`

## Ejecución local
```bash
python src/main.py input.example.json
```

## Notas
- No envía correos en v1.
- Guarda resultados JSON en `artifacts/`.
- Puede cambiar de tenant sin editar código usando `.env` + `config/tools/graph/profiles.json`.
