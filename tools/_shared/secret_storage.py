"""
Encriptación at-rest para tokens de Microsoft Identity Platform.

Diseño (envelope encryption):
- Metadata no sensible (saved_at, expires_in, profile_name, tenant_id,
  resource, scope) queda en plaintext en el JSON. Esto permite que
  is_expired(), list_tokens() y similares funcionen sin descifrar nada.
- Campos sensibles (access_token, refresh_token, id_token) se cifran
  con Fernet (AES-128-CBC + HMAC-SHA256, autenticado) y se almacenan
  como un único blob `encrypted_secrets`.
- La clave Fernet vive en AGENTEC_TOKEN_ENCRYPTION_KEY (env var, lo
  más simple) o AGENTEC_TOKEN_ENCRYPTION_KEY_FILE (path a archivo,
  útil si quieres restringir el acceso vía permisos del host).

Modes:
- Sin clave configurada: degradación a plaintext (backward compat).
  Loguea warning a stderr.
- Sin clave + AGENTEC_REQUIRE_ENCRYPTION=1: error fatal. Usar este
  modo en producción para evitar accidentes.

Generar una nueva clave:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Guardarla en stack.env (NO en el repo) o en /run/secrets/token_key (mount Docker secret).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Lazy-import: si el contenedor no tiene `cryptography` instalado y el operador
# no pidió encriptación, el módulo debe seguir importable para no romper el
# runtime existente.
_FERNET = None
_FERNET_LOAD_ATTEMPTED = False


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_key_material() -> bytes | None:
    """Devuelve la clave Fernet en bytes, o None si no hay clave configurada.

    Prioridad:
    1. AGENTEC_TOKEN_ENCRYPTION_KEY (env var directa)
    2. AGENTEC_TOKEN_ENCRYPTION_KEY_FILE (path a archivo con la clave)
    """
    raw = os.environ.get("AGENTEC_TOKEN_ENCRYPTION_KEY", "").strip()
    if raw:
        return raw.encode()

    key_file = os.environ.get("AGENTEC_TOKEN_ENCRYPTION_KEY_FILE", "").strip()
    if key_file:
        path = Path(key_file).expanduser()
        if not path.exists():
            print(
                f"[secret_storage] AGENTEC_TOKEN_ENCRYPTION_KEY_FILE apunta a "
                f"{path!s} que no existe",
                file=sys.stderr,
            )
            return None
        # Validar permisos del archivo: si es world-readable, alertar.
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                print(
                    f"[secret_storage] WARN: {path!s} tiene permisos {oct(mode)} — "
                    f"recomendado 0o600. Cualquier proceso/usuario con acceso "
                    f"al filesystem puede leer la clave.",
                    file=sys.stderr,
                )
        except OSError:
            pass
        return path.read_text(encoding="utf-8").strip().encode()

    return None


def _get_fernet():
    """Lazy-construct el objeto Fernet. Memoiza para no re-leer la clave en cada I/O."""
    global _FERNET, _FERNET_LOAD_ATTEMPTED
    if _FERNET is not None:
        return _FERNET
    if _FERNET_LOAD_ATTEMPTED:
        return None
    _FERNET_LOAD_ATTEMPTED = True

    key = _load_key_material()
    if not key:
        if _bool_env("AGENTEC_REQUIRE_ENCRYPTION"):
            raise RuntimeError(
                "CONFIG_ERROR: AGENTEC_REQUIRE_ENCRYPTION=1 pero no hay clave "
                "configurada. Define AGENTEC_TOKEN_ENCRYPTION_KEY o "
                "AGENTEC_TOKEN_ENCRYPTION_KEY_FILE."
            )
        print(
            "[secret_storage] WARN: encriptación de tokens deshabilitada "
            "(AGENTEC_TOKEN_ENCRYPTION_KEY no configurada). Los tokens se "
            "guardarán en plaintext.",
            file=sys.stderr,
        )
        return None

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "CONFIG_ERROR: AGENTEC_TOKEN_ENCRYPTION_KEY está configurada pero "
            "la librería `cryptography` no está instalada. Agrégala a "
            "requirements.txt."
        ) from exc

    try:
        _FERNET = Fernet(key)
    except ValueError as exc:
        raise RuntimeError(
            f"CONFIG_ERROR: AGENTEC_TOKEN_ENCRYPTION_KEY inválida: {exc}. "
            "Debe ser una clave Fernet válida (32 bytes random codificados en "
            "base64). Genera una con: "
            "python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc
    return _FERNET


def encryption_enabled() -> bool:
    """True si los tokens nuevos se cifrarán al guardar."""
    return _get_fernet() is not None


# Campos del response del IdP que SIEMPRE deben cifrarse.
# Si Microsoft agrega más en el futuro (ej. una nueva forma de credencial),
# extender esta lista.
SENSITIVE_FIELDS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
})


def save_token_file(path: Path, token_data: dict[str, Any]) -> None:
    """Persiste un token a disco. Cifra los campos sensibles si hay clave configurada.

    El formato del archivo cifrado es:
        {
          "version": 1,
          "encrypted": true,
          "saved_at": ..., "profile_name": ..., ...,  # metadata plaintext
          "encrypted_secrets": "gAAAAA..."             # access/refresh/id tokens cifrados
        }

    El formato del archivo plaintext (legacy / modo sin clave) es el original:
    JSON con todos los campos en claro.
    """
    fernet = _get_fernet()
    if fernet is None:
        # Modo plaintext (backward compat). No reescribimos el shape.
        path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return

    sensitive_payload = {
        k: v for k, v in token_data.items()
        if k in SENSITIVE_FIELDS and v is not None
    }
    metadata = {
        k: v for k, v in token_data.items()
        if k not in SENSITIVE_FIELDS
    }

    encrypted_blob = fernet.encrypt(
        json.dumps(sensitive_payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    envelope = {
        "version": 1,
        "encrypted": True,
        **metadata,
        "encrypted_secrets": encrypted_blob,
    }
    path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_token_file(path: Path) -> dict[str, Any] | None:
    """Lee un token de disco. Maneja tanto formato cifrado como plaintext legacy.

    Si el archivo es plaintext y hay clave configurada, NO lo migra
    automáticamente — la migración ocurre en el próximo save_token_file()
    (cuando se rota el refresh_token o se guarda un access_token nuevo).
    Esto evita riesgos de re-escritura sin necesidad.
    """
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))

    if not data.get("encrypted"):
        # Plaintext legacy. Devolver tal cual.
        return data

    encrypted_blob = data.pop("encrypted_secrets", None)
    data.pop("encrypted", None)
    data.pop("version", None)

    if not encrypted_blob:
        # Envelope marcado como cifrado pero sin payload. Corrupto.
        raise RuntimeError(
            f"AUTH_ERROR: archivo de token {path.name} marcado como cifrado "
            "pero sin payload `encrypted_secrets`. Borra el archivo y re-autentica."
        )

    fernet = _get_fernet()
    if fernet is None:
        raise RuntimeError(
            f"AUTH_ERROR: archivo de token {path.name} está cifrado pero no hay "
            "clave configurada. Define AGENTEC_TOKEN_ENCRYPTION_KEY con la clave "
            "que se usó para cifrar, o borra el archivo y re-autentica."
        )

    from cryptography.fernet import InvalidToken
    try:
        plain = fernet.decrypt(encrypted_blob.encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError(
            f"AUTH_ERROR: no se puede descifrar {path.name} con la clave actual. "
            "Es probable que la clave haya rotado o que el archivo sea de otro "
            "tenant. Re-ejecuta auth-login."
        ) from exc

    secrets_dict = json.loads(plain.decode("utf-8"))
    data.update(secrets_dict)
    return data


# ─── Sanitización para logs y artifacts ───────────────────────────────────────
# El runtime escribe artifacts JSON con cada llamada. Si por error un payload
# incluyera tokens (ej. response del IdP en un error path), quedarían en disco
# en plaintext aunque los token files estén cifrados. Esta función se llama
# defensivamente antes de serializar cualquier cosa a artifact/log.

_REDACTED = "[REDACTED]"


def redact_secrets(value: Any) -> Any:
    """Devuelve una copia de `value` con campos sensibles reemplazados por [REDACTED].

    Aplica recursivamente a dicts y listas. No muta el input. Usar antes de
    escribir cualquier estructura a artifact o log que pueda haber recogido
    respuestas del IdP.

    Campos detectados (case-insensitive sobre el nombre de la key):
    - access_token, refresh_token, id_token, client_secret, password, code,
      device_code, user_code, authorization, secret, bearer
    """
    blocked_keys = {
        "access_token", "refresh_token", "id_token",
        "client_secret", "password", "secret", "bearer",
        "device_code", "code", "authorization",
        # user_code aparece en device flow pero es de un solo uso y se muestra
        # al usuario en pantalla — NO lo redactamos para no romper el UX.
    }

    if isinstance(value, dict):
        return {
            k: _REDACTED if k.lower() in blocked_keys else redact_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


# ─── CLI auxiliar para generar y validar claves ─────────────────────────────
def _cli_main():
    """python3 -m _shared.secret_storage gen-key | check"""
    if len(sys.argv) < 2 or sys.argv[1] not in {"gen-key", "check"}:
        print("Uso: python3 -m _shared.secret_storage [gen-key|check]", file=sys.stderr)
        sys.exit(2)

    if sys.argv[1] == "gen-key":
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            print("Necesitas `pip install cryptography`", file=sys.stderr)
            sys.exit(1)
        print(Fernet.generate_key().decode())
        return

    # check
    if encryption_enabled():
        print("OK: encriptación habilitada con clave válida")
    else:
        print("WARN: encriptación deshabilitada (no hay clave configurada)")
        sys.exit(1)


if __name__ == "__main__":
    _cli_main()