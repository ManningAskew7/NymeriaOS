"""Streaming passphrase encryption for user-data snapshot artifacts.

The snapshot feature (``core/snapshot.py``) writes a tar.gz stream through the
encrypting writer below, producing a single self-contained ``.nysnap`` file
that embeds the deployment's Fernet vault key. The envelope therefore has to
stand on its own passphrase, not on any key already present on the host:

- Key derivation: scrypt over the operator passphrase with a random per-file
  salt (parameters recorded in the plaintext header, so they can be tuned
  later without breaking old artifacts).
- Cipher: AES-256-GCM applied per chunk, so create/verify/restore stream in
  constant memory instead of holding the whole archive.
- Framing: every frame is ``u32 length + ciphertext``; the plaintext of each
  frame carries a 1-byte type tag (data or final). Nonces are a 96-bit
  big-endian frame counter, which is safe because the scrypt salt makes the
  key unique per artifact, and which makes reordered, duplicated, or dropped
  frames fail authentication. Truncation is detected by the absence of the
  final frame.

``cryptography`` is already a core dependency (it backs ``core/secrets.py``);
nothing new is required. This module is stdlib + cryptography only and imports
nothing from the rest of the package, so the CLI can use it before settings
load.
"""

from __future__ import annotations

import io
import json
import os
import struct
from pathlib import Path
from typing import BinaryIO, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"NYSNAP\x01E"
FORMAT_VERSION = 1

# scrypt parameters: ~32 MiB, interactive-grade. Recorded in the header so
# future tuning does not orphan existing artifacts.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024
# A frame is chunk plaintext + 1 type byte + 16-byte GCM tag; anything far
# beyond that in a length prefix means corruption, so cap what we allocate.
_FRAME_OVERHEAD = 1 + 16

_TYPE_DATA = b"\x00"
_TYPE_FINAL = b"\x01"


class SnapshotCryptoError(RuntimeError):
    """Base error for snapshot envelope problems."""


class SnapshotDecryptError(SnapshotCryptoError):
    """Wrong passphrase, tampered data, or a truncated artifact."""


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not passphrase:
        raise SnapshotCryptoError("A non-empty passphrase is required")
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def _nonce(counter: int) -> bytes:
    return counter.to_bytes(12, "big")


class EncryptingWriter(io.RawIOBase):
    """File-like writer that frames and encrypts everything written to it.

    ``close()`` flushes the trailing partial chunk and appends the final
    frame; it does NOT close the underlying file object (the caller owns it,
    and typically still needs to fsync/rename it).
    """

    def __init__(
        self,
        fileobj: BinaryIO,
        passphrase: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        super().__init__()
        if chunk_size <= 0:
            raise SnapshotCryptoError("chunk_size must be positive")
        salt = os.urandom(_SALT_BYTES)
        header = {
            "v": FORMAT_VERSION,
            "kdf": "scrypt",
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "salt": salt.hex(),
            "cipher": "aes-256-gcm",
            "chunk": chunk_size,
        }
        self._header_bytes = json.dumps(header, separators=(",", ":")).encode("ascii")
        self._aead = AESGCM(
            _derive_key(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        )
        self._out = fileobj
        self._chunk_size = chunk_size
        self._buffer = bytearray()
        self._counter = 0
        self._finalized = False
        self._out.write(MAGIC)
        self._out.write(struct.pack(">I", len(self._header_bytes)))
        self._out.write(self._header_bytes)

    def writable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def write(self, data) -> int:  # type: ignore[override]
        if self._finalized:
            raise ValueError("write to a finalized EncryptingWriter")
        self._buffer.extend(data)
        while len(self._buffer) >= self._chunk_size:
            chunk = bytes(self._buffer[: self._chunk_size])
            del self._buffer[: self._chunk_size]
            self._emit(_TYPE_DATA + chunk)
        return len(data)

    def _emit(self, plaintext: bytes) -> None:
        ciphertext = self._aead.encrypt(
            _nonce(self._counter), plaintext, self._header_bytes
        )
        self._counter += 1
        self._out.write(struct.pack(">I", len(ciphertext)))
        self._out.write(ciphertext)

    def close(self) -> None:
        if self.closed:
            return
        if not self._finalized:
            if self._buffer:
                self._emit(_TYPE_DATA + bytes(self._buffer))
                self._buffer.clear()
            self._emit(_TYPE_FINAL)
            self._finalized = True
        super().close()


class DecryptingReader(io.RawIOBase):
    """File-like reader that verifies and decrypts an encrypted artifact.

    Raises :class:`SnapshotDecryptError` on a bad passphrase, any tampering
    (frame reorder/drop/duplicate included, via the counter nonce), and on
    truncation (EOF before the final frame).
    """

    def __init__(self, fileobj: BinaryIO, passphrase: str) -> None:
        super().__init__()
        self._in = fileobj
        magic = fileobj.read(len(MAGIC))
        if magic != MAGIC:
            raise SnapshotDecryptError(
                "Not an encrypted snapshot artifact (bad magic bytes)"
            )
        header_len_raw = fileobj.read(4)
        if len(header_len_raw) != 4:
            raise SnapshotDecryptError("Truncated artifact header")
        (header_len,) = struct.unpack(">I", header_len_raw)
        if header_len > _MAX_HEADER_BYTES:
            raise SnapshotDecryptError("Corrupt artifact header (oversized)")
        self._header_bytes = fileobj.read(header_len)
        if len(self._header_bytes) != header_len:
            raise SnapshotDecryptError("Truncated artifact header")
        try:
            header = json.loads(self._header_bytes)
            salt = bytes.fromhex(header["salt"])
            n, r, p = int(header["n"]), int(header["r"]), int(header["p"])
            chunk = int(header["chunk"])
            if header.get("v") != FORMAT_VERSION:
                raise SnapshotDecryptError(
                    f"Unsupported snapshot format version: {header.get('v')!r}"
                )
            if header.get("cipher") != "aes-256-gcm" or header.get("kdf") != "scrypt":
                raise SnapshotDecryptError(
                    "Unsupported snapshot cipher/KDF in header"
                )
        except SnapshotDecryptError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise SnapshotDecryptError("Corrupt artifact header") from exc
        if chunk <= 0 or chunk > 256 * 1024 * 1024:
            raise SnapshotDecryptError("Corrupt artifact header (bad chunk size)")
        self._max_frame = chunk + _FRAME_OVERHEAD
        self._aead = AESGCM(_derive_key(passphrase, salt, n, r, p))
        self._counter = 0
        self._eof = False
        self._buffer = bytearray()

    def readable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def _read_exact(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            piece = self._in.read(size - len(out))
            if not piece:
                raise SnapshotDecryptError(
                    "Truncated artifact (ended mid-frame before the final marker)"
                )
            out.extend(piece)
        return bytes(out)

    def _pull_frame(self) -> None:
        length_raw = self._in.read(4)
        if not length_raw:
            raise SnapshotDecryptError(
                "Truncated artifact (missing final marker frame)"
            )
        if len(length_raw) != 4:
            raise SnapshotDecryptError("Truncated artifact (partial frame length)")
        (length,) = struct.unpack(">I", length_raw)
        if length < _FRAME_OVERHEAD or length > self._max_frame:
            raise SnapshotDecryptError("Corrupt artifact (implausible frame length)")
        ciphertext = self._read_exact(length)
        try:
            plaintext = self._aead.decrypt(
                _nonce(self._counter), ciphertext, self._header_bytes
            )
        except InvalidTag as exc:
            hint = (
                "wrong passphrase or corrupted artifact"
                if self._counter == 0
                else "corrupted or tampered artifact"
            )
            raise SnapshotDecryptError(f"Decryption failed: {hint}") from exc
        self._counter += 1
        if not plaintext:
            raise SnapshotDecryptError("Corrupt artifact (empty frame)")
        frame_type, payload = plaintext[:1], plaintext[1:]
        if frame_type == _TYPE_FINAL:
            if payload:
                raise SnapshotDecryptError("Corrupt artifact (non-empty final frame)")
            # Anything after the final frame is unauthenticated trailing junk.
            if self._in.read(1):
                raise SnapshotDecryptError(
                    "Corrupt artifact (data after the final marker)"
                )
            self._eof = True
            return
        if frame_type != _TYPE_DATA:
            raise SnapshotDecryptError("Corrupt artifact (unknown frame type)")
        self._buffer.extend(payload)

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            chunks = []
            while True:
                piece = self.read(io.DEFAULT_BUFFER_SIZE)
                if not piece:
                    return b"".join(chunks)
                chunks.append(piece)
        while len(self._buffer) < size and not self._eof:
            self._pull_frame()
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out


def open_encrypting_writer(
    fileobj: BinaryIO,
    passphrase: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> EncryptingWriter:
    return EncryptingWriter(fileobj, passphrase, chunk_size=chunk_size)


def open_decrypting_reader(fileobj: BinaryIO, passphrase: str) -> DecryptingReader:
    return DecryptingReader(fileobj, passphrase)


def sniff_artifact(path: Path) -> Optional[str]:
    """Classify an artifact file: ``"encrypted"``, ``"plain"`` (gzip), or None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(len(MAGIC))
    except OSError:
        return None
    if head == MAGIC:
        return "encrypted"
    if head[:2] == b"\x1f\x8b":
        return "plain"
    return None


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DecryptingReader",
    "EncryptingWriter",
    "FORMAT_VERSION",
    "MAGIC",
    "SnapshotCryptoError",
    "SnapshotDecryptError",
    "open_decrypting_reader",
    "open_encrypting_writer",
    "sniff_artifact",
]
