# agentec-tools

Repositorio central de tools ejecutables para AgenTEC/OpenClaw. Cada tool es un proceso independiente (Docker) con su propio schema de entrada/salida, invocado por el servidor MCP de `agentec-catalog` cuando OpenClaw lo solicita.

---

## Posición en el ecosistema

```
agentec-tools           ← este repo
  tools/<nombre>/
    tool.yaml           → schema, runtime y entrypoint
    src/main.py         → implementación (Python)
    src/index.ts        → implementación (TypeScript/Node.js)
    Dockerfile          → imagen standalone
    tool.yaml
  tools/_shared/
    graph_runtime.py    → runtime compartido para todas las tools Graph
    secret_storage.py   → encriptación at-rest de tokens (Fernet)

        │  montado como volumen :ro en
        ▼
agentec-catalog / mcp-server
  → run-external-tool.ts invoca el proceso con spawn()
  → aprobadas en tools/approved-tools.yaml

        │
        ▼
agentec-openclaw-stack → OpenClaw gateway
```

Las tools **no tienen servidor permanente**: el servidor MCP las invoca como procesos one-shot (`spawn`) pasando un archivo JSON de entrada y leyendo el JSON de salida de stdout.

---

## Catálogo de tools

### Web / Automatización de navegador

| Tool | Runtime | Entrypoint | Descripción |
|---|---|---|---|
| `web-login-playwright` | Node.js | `node dist/index.js` | Login web con Playwright. Genera screenshot y JSON de evidencia. Soporta perfiles configurables de portal. |
| `web-login-playwright-py` | Python | `python src/main.py` | Login web con Playwright (backend Python). Misma funcionalidad que el Node, útil para shadow mode y depuración local. |
| `web-fetch-download` | Node.js | `node dist/index.js` | Flujos web multistep: descarga de documentos detrás de login/navegación y extracción de `videoId` de YouTube. |

### Documentos y contenido

| Tool | Runtime | Entrypoint | Descripción |
|---|---|---|---|
| `doc-reader` | Python | `python src/main.py` | Extrae texto estructurado de archivos locales (PDF, DOCX, XLSX, TXT, MD). Devuelve contenido, conteo de páginas y palabras. |
| `curp-downloader` | Python | `python src/main.py` | Descarga el comprobante CURP (PDF) desde gob.mx/curp vía Playwright. Dos modos: por clave CURP o por datos personales. Entrega como artifact local, adjunto de correo o archivo en OneDrive. |

### Microsoft 365 / Microsoft Graph

Todas las tools Graph comparten el runtime `_shared/graph_runtime.py` y el mismo flujo de autenticación device code. Ver [Autenticación Graph](#auth-graph).

| Tool | Descripción | Acciones principales |
|---|---|---|
| `graph-mail` | Correo Microsoft 365 | `unread`, `recent`, `read`, `send`, `reply`, `forward`, `delete`, `mark_read`, `folders`, `move`, `search`, `thread`, `radar`, `digest` |
| `graph-files` | Archivos OneDrive / SharePoint (lectura) | `recent`, `search`, `read`, `summarize`, `auth-login`, `auth-poll` |
| `graph-files-write` | Archivos OneDrive / SharePoint (escritura) | `upload`, `create_folder`, `rename`, `move`, `copy`, `delete`, `share` |
| `graph-calendar` | Calendario Microsoft 365 | `today`, `week`, `month`, `create`, `update`, `delete`, `free-busy` |
| `graph-teams` | Microsoft Teams | `teams`, `channels`, `messages`, `send`, `members` |
| `graph-users` | Directorio y organigrama Entra ID | `search`, `list`, `me`, `manager`, `reports`, `by-department` |
| `graph-sharepoint-search` | Microsoft Search API (SharePoint + OneDrive) | `search`, `list-sites` |
| `graph-approvals` | Aprobaciones de Power Automate | `pending`, `all`, `history` |
| `graph-flows` | Flujos de Power Automate | `list`, `read`, `runs`, `trigger`, `enable`, `disable` |
| `graph-powerbi` | Power BI workspaces, reportes y datasets | `workspaces`, `reports`, `dashboards`, `datasets`, `query` (DAX), `open`, `pages`, `tiles`, `refresh` |

### Operaciones del stack

| Tool | Runtime | Descripción |
|---|---|---|
| `cleanup` | Python | Limpia artifacts (JSONs, screenshots), logs y archivos temporales. Acciones: `status`, `artifacts`, `logs`, `purge`. Siempre opera en `dry_run=true` por defecto. |

---

## Infraestructura compartida (`tools/_shared/`)

### `graph_runtime.py`

Runtime Python compartido por todas las tools Graph. Provee:

- **Autenticación device code** — `init_login()` / `poll_login()` contra Microsoft Identity Platform
- **Gestión de tokens** — refresh automático, detección de expiración, `get_valid_token()`
- **HTTP helpers** — `graph_get_json()`, `graph_post_json()`, etc.
- **Mensajes de error orientados al agente** — prefijos `AUTH_ERROR:`, `GRAPH_ERROR:` que las skills usan para routing sin mencionar comandos shell
- **Resolución de configuración** — `resolve_graph_settings()` lee el `profiles.json` del stack

### `secret_storage.py`

Encriptación at-rest de tokens de Microsoft Identity Platform usando **Fernet (AES-128-CBC + HMAC-SHA256)**:

- Los campos sensibles (`access_token`, `refresh_token`, `id_token`) se cifran en un blob `encrypted_secrets`
- Los metadatos no sensibles (fechas, scopes, profile) quedan en plaintext para que `is_expired()` funcione sin descifrar
- Clave configurada vía `AGENTEC_TOKEN_ENCRYPTION_KEY` (env var) o `AGENTEC_TOKEN_ENCRYPTION_KEY_FILE`
- Sin clave: degrada a plaintext con warning. Con `AGENTEC_REQUIRE_ENCRYPTION=1`: error fatal (recomendado en producción)

```bash
# Generar una nueva clave
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

<a id="auth-graph"></a>
## Autenticación Microsoft Graph

Todas las tools Graph aceptan `action: "auth-login"` y `action: "auth-poll"` como acciones especiales de autenticación. El flujo es:

```
1. tool({ action: "auth-login", profile: "<nombre>" })
   → { verification_uri, user_code, expires_in }

2. El agente muestra la URL y el código al usuario y espera confirmación.

3. tool({ action: "auth-poll", profile: "<nombre>" })
   → { status: "ok" }  → tokens guardados en /app/graph-tokens/<profile>/<user>.json
   → { status: "pending" }  → aún no confirmado
   → { status: "expired" }  → reiniciar con auth-login
```

Los tokens se almacenan cifrados (ver `secret_storage.py`) y se renuevan automáticamente con el refresh token. La sesión de `graph-flows` también sirve para `graph-approvals` y `graph-powerbi` sin segundo login (MRRT).

---

## Estructura de una tool

```
tools/<nombre>/
├── tool.yaml             # nombre, versión, runtime, entrypoint, input/output schema (requerido)
├── src/
│   └── main.py           # implementación Python (o index.ts para Node.js)
├── Dockerfile            # imagen Docker standalone
├── pyproject.toml        # dependencias Python (o package.json para Node.js)
├── input.example.json    # ejemplo de entrada para pruebas manuales
└── README.md             # documentación específica de la tool (opcional)
```

### `tool.yaml` mínimo

```yaml
name: mi-tool
version: 0.1.0
type: tool
runtime: docker
entrypoint: python src/main.py /input.json
description: descripción breve
input_schema:
  type: object
  required: [accion]
  properties:
    accion:
      type: string
output_schema:
  type: object
  properties:
    success:
      type: boolean
    message:
      type: string
```

---

## Agregar una nueva tool

1. Crear el directorio `tools/<nombre>/`.
2. Crear `tool.yaml` con el schema de entrada/salida.
3. Implementar `src/main.py` (Python) o `src/index.ts` (TypeScript). La tool debe leer el JSON de entrada desde el archivo pasado como argumento y escribir el JSON de salida en stdout.
4. Crear `Dockerfile` con la imagen standalone.
5. Registrar el handler en `agentec-catalog/mcp-server/src/registry.ts` → `HANDLER_MAP`.
6. Añadir el handler TypeScript en `agentec-catalog/mcp-server/src/tools/<nombre>.ts`.
7. Aprobar la tool en `agentec-catalog/tools/approved-tools.yaml` con `status: approved`.

Si la tool necesita autenticación Microsoft Graph, importar el runtime compartido:

```python
from graph_runtime import get_valid_token, init_login, poll_login, build_success_result, build_error_result
```

---

## Variables de entorno relevantes

| Variable | Descripción |
|---|---|
| `AGENTEC_TOOLS_DIR` | Ruta raíz de este repo montada en el contenedor MCP (`/app/external-tools`) |
| `AGENTEC_SHARED_DIR` | Override explícito de la ruta a `_shared/` |
| `AGENTEC_GRAPH_TOKEN_STORE_DIR` | Directorio donde se almacenan los tokens Graph cifrados |
| `AGENTEC_TOKEN_ENCRYPTION_KEY` | Clave Fernet para encriptar tokens at-rest |
| `AGENTEC_REQUIRE_ENCRYPTION` | Si es `1`, la tool falla si no hay clave de encriptación configurada |
| `AGENTEC_GRAPH_PROFILE` | Perfil Graph activo por defecto |
| `AGENTEC_GRAPH_CONFIG_FILE` | Ruta al archivo `profiles.json` con la configuración de tenants |

La configuración sensible (tenant IDs, client IDs, secretos) vive en el `.env` y `config/` de `agentec-openclaw-stack`, nunca en este repositorio.

---

## Repositorios relacionados

| Repo | Rol |
|---|---|
| [agentec-catalog](https://github.com/tluisguereroItesm/agentec-catalog) | Servidor MCP que invoca estas tools; lista de aprobación |
| [agentec-skills](https://github.com/tluisguereroItesm/agentec-skills) | Instrucciones para el agente sobre cuándo y cómo usar cada tool |
| [agentec-openclaw-stack](https://github.com/tluisguereroItesm/agentec-openclaw-stack) | Monta este repo como volumen en el contenedor MCP y levanta el stack completo |
| [openclaw](https://github.com/openclaw/openclaw) | Gateway de IA que consume el servidor MCP |
