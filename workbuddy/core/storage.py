"""账号池的本地加密存储。

整份账号池文档用 AES-256-GCM 加密后原子写入（同目录临时文件 + ``os.replace``）。
主密钥优先取环境变量 ``WORKBUDDY_MASTER_KEY``（标准 base64、解码后 32 字节），
缺省时在数据目录生成 ``.workbuddy_master_key``（0600）。换机时需要把该文件
与 ``accounts.enc`` 一起拷走。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"WBPL"
_VERSION = b"\x01"
_PREFIX = _MAGIC + _VERSION
_NONCE_BYTES = 12
_KEY_BYTES = 32

_ERR_KEY = "credential master key must be base64 of exactly 32 bytes"
_ERR_DECRYPT = "stored accounts could not be decrypted"
_ERR_PARSE = "stored accounts could not be parsed"

MASTER_KEY_ENV = "WORKBUDDY_MASTER_KEY"
MASTER_KEY_FILENAME = ".workbuddy_master_key"


def _decode_master_key(master_key: str) -> bytes:
    try:
        raw = base64.b64decode(master_key, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError(_ERR_KEY) from None
    if len(raw) != _KEY_BYTES:
        raise ValueError(_ERR_KEY)
    return raw


def load_or_create_master_key(data_dir: Path) -> str:
    """返回可用的主密钥：环境变量优先，否则复用/生成密钥文件。"""
    env_key = (os.environ.get(MASTER_KEY_ENV) or "").strip()
    if env_key:
        _decode_master_key(env_key)  # 提前校验，避免首次落盘才发现
        return env_key

    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / MASTER_KEY_FILENAME
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        _decode_master_key(stored)
        return stored

    key = base64.b64encode(secrets.token_bytes(_KEY_BYTES)).decode()
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def _atomic_write(directory: Path, target: Path, blob: bytes, prefix: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class EncryptedJsonStore:
    """单个 JSON 文档的加密存储。"""

    def __init__(self, path: str | os.PathLike[str], master_key: str) -> None:
        self._path = Path(path)
        self._dir = self._path.parent
        self._key = _decode_master_key(master_key)
        self._aad = _PREFIX

    @property
    def path(self) -> Path:
        return self._path

    def save(self, document: dict) -> None:
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, payload, self._aad)
        _atomic_write(self._dir, self._path, _PREFIX + nonce + ciphertext, ".wb-accounts-")

    def load(self) -> dict | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        if not raw.startswith(_PREFIX) or len(raw) < len(_PREFIX) + _NONCE_BYTES:
            raise ValueError(_ERR_DECRYPT)
        nonce = raw[len(_PREFIX):len(_PREFIX) + _NONCE_BYTES]
        ciphertext = raw[len(_PREFIX) + _NONCE_BYTES:]
        try:
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, self._aad)
        except Exception:
            raise ValueError(_ERR_DECRYPT) from None
        try:
            obj = json.loads(plaintext.decode("utf-8"))
        except Exception:
            raise ValueError(_ERR_PARSE) from None
        if not isinstance(obj, dict):
            raise ValueError(_ERR_PARSE)
        return obj

    def delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
