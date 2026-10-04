"""box.py -- authenticated encryption for the archive and for results.

AES-256-GCM with a key derived from a passphrase by scrypt. Every sealed blob
carries its own random salt and nonce, so one passphrase (kept out of this repo)
unseals anything sealed with it. A wrong passphrase, or any tampering, fails
loudly: GCM authentication raises before any plaintext is produced.

Blob layout: MAGIC | version(1) | salt(16) | nonce(12) | ciphertext+tag.
A folder is sealed by bundling it into a deterministic, metadata-stripped tar and
sealing the bytes; unsealing reverses it.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"LABX"
VERSION = 1
_SALT = 16
_NONCE = 12
_N, _R, _P = 2 ** 15, 8, 1     # scrypt cost; fixed so any holder of the passphrase can unseal


class BadKey(Exception):
    """Raised when a blob cannot be authenticated (wrong passphrase or tampering)."""


def _derive(passphrase: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_N, r=_R, p=_P).derive(passphrase)


def seal_bytes(data: bytes, passphrase: bytes) -> bytes:
    import os
    salt, nonce = os.urandom(_SALT), os.urandom(_NONCE)
    key = _derive(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return MAGIC + bytes([VERSION]) + salt + nonce + ct


def unseal_bytes(blob: bytes, passphrase: bytes) -> bytes:
    if blob[:len(MAGIC)] != MAGIC or blob[len(MAGIC)] != VERSION:
        raise BadKey("not a recognised sealed blob")
    off = len(MAGIC) + 1
    salt = blob[off:off + _SALT]
    nonce = blob[off + _SALT:off + _SALT + _NONCE]
    ct = blob[off + _SALT + _NONCE:]
    key = _derive(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as e:                      # InvalidTag and friends
        raise BadKey("wrong passphrase or corrupted data") from e


def _bundle(src: Path) -> bytes:
    buf = io.BytesIO()
    files = sorted(p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in files:
            info = tarfile.TarInfo(p.relative_to(src).as_posix())
            data = p.read_bytes()
            info.size = len(data)
            info.mtime = 0                      # strip timestamps for a reproducible archive
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unbundle(data: bytes, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for info in tar.getmembers():
            target = (dst / info.name).resolve()
            if not str(target).startswith(str(dst.resolve())):
                raise BadKey("archive member escapes the destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(info) as fh:
                target.write_bytes(fh.read())


def seal_dir(src: Path, passphrase: bytes) -> bytes:
    return seal_bytes(_bundle(Path(src)), passphrase)


def unseal_dir(blob: bytes, dst: Path, passphrase: bytes):
    _unbundle(unseal_bytes(blob, passphrase), Path(dst))


def read_passphrase(source) -> bytes:
    """A passphrase from a file path or from the string itself. The file holds a
    single line; surrounding whitespace is stripped."""
    p = Path(source)
    if p.exists():
        return p.read_text(encoding="utf-8").strip().encode()
    return str(source).strip().encode()
