"""Bóveda de credenciales de brokers.

Cifrado de sobre (envelope encryption):
  - Cada conexion tiene su propia clave de datos (DEK) de 256 bits, generada al azar.
  - Los datos se cifran con la DEK usando AES-256-GCM (via Fernet).
  - La DEK se cifra con la clave maestra (KEK), que viene de la variable de entorno
    METABOT_VAULT_KEY. La KEK nunca se guarda en la base de datos ni en el codigo.

Para produccion la KEK debe vivir en un KMS (AWS KMS, GCP KMS o Vault); esta
implementacion la lee del entorno como paso intermedio.
"""
import base64
import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

KEY_VERSION = 1


class VaultError(RuntimeError):
    pass


def _master_key() -> Fernet:
    raw = os.environ.get("METABOT_VAULT_KEY", "").strip()
    if not raw:
        raise VaultError("Falta METABOT_VAULT_KEY en el entorno")
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as e:
        raise VaultError("METABOT_VAULT_KEY no es una clave Fernet valida") from e


def new_master_key() -> str:
    """Genera una clave maestra nueva. Se ejecuta una sola vez, fuera del servidor."""
    return Fernet.generate_key().decode()


def seal(payload: dict[str, Any]) -> tuple[bytes, bytes, int]:
    """Devuelve (datos_cifrados, dek_cifrada, version_de_clave)."""
    dek = Fernet.generate_key()
    data_blob = Fernet(dek).encrypt(json.dumps(payload).encode())
    wrapped_dek = _master_key().encrypt(dek)
    return data_blob, wrapped_dek, KEY_VERSION


def open_sealed(data_blob: bytes, wrapped_dek: bytes, key_version: int) -> dict[str, Any]:
    if key_version != KEY_VERSION:
        raise VaultError(f"Version de clave no soportada: {key_version}")
    try:
        dek = _master_key().decrypt(wrapped_dek)
        return json.loads(Fernet(dek).decrypt(data_blob).decode())
    except InvalidToken as e:
        raise VaultError("No se pudo descifrar: clave incorrecta o datos alterados") from e
