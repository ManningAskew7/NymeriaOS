"""Tests for the snapshot encryption envelope (core/snapshot_crypto.py)."""

import io

import pytest

from nymeria.core.snapshot_crypto import (
    MAGIC,
    SnapshotDecryptError,
    open_decrypting_reader,
    open_encrypting_writer,
    sniff_artifact,
)


def _encrypt(payload: bytes, passphrase: str, *, chunk_size: int = 1024) -> bytes:
    sink = io.BytesIO()
    writer = open_encrypting_writer(sink, passphrase, chunk_size=chunk_size)
    writer.write(payload)
    writer.close()
    return sink.getvalue()


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    reader = open_decrypting_reader(io.BytesIO(blob), passphrase)
    return reader.read()


def test_roundtrip_small_payload():
    payload = b"hello snapshot"
    assert _decrypt(_encrypt(payload, "pw-123456"), "pw-123456") == payload


def test_roundtrip_multi_chunk_and_exact_boundary():
    # Cross several chunk boundaries, including an exact-multiple size.
    for size in (1024, 1024 * 3, 1024 * 3 + 17, 1):
        payload = bytes(range(256)) * (size // 256 + 1)
        payload = payload[:size]
        assert _decrypt(_encrypt(payload, "pw-123456"), "pw-123456") == payload


def test_roundtrip_empty_payload():
    assert _decrypt(_encrypt(b"", "pw-123456"), "pw-123456") == b""


def test_incremental_reads_match():
    payload = b"x" * 5000
    blob = _encrypt(payload, "pw-123456")
    reader = open_decrypting_reader(io.BytesIO(blob), "pw-123456")
    out = bytearray()
    while True:
        piece = reader.read(313)
        if not piece:
            break
        out.extend(piece)
    assert bytes(out) == payload


def test_wrong_passphrase_rejected():
    blob = _encrypt(b"data", "correct-horse")
    with pytest.raises(SnapshotDecryptError, match="wrong passphrase"):
        _decrypt(blob, "battery-staple")


def test_truncated_artifact_rejected():
    blob = _encrypt(b"y" * 4000, "pw-123456")
    with pytest.raises(SnapshotDecryptError, match="[Tt]runcated"):
        _decrypt(blob[: len(blob) - 30], "pw-123456")


def test_missing_final_marker_rejected():
    # Cut exactly at a frame boundary: strip the last (final-marker) frame,
    # which is 4 bytes length prefix + 17 bytes ciphertext.
    blob = _encrypt(b"z" * 1024, "pw-123456", chunk_size=1024)
    with pytest.raises(SnapshotDecryptError, match="final"):
        _decrypt(blob[: len(blob) - 21], "pw-123456")


def test_tampered_chunk_rejected():
    blob = bytearray(_encrypt(b"q" * 3000, "pw-123456"))
    # Flip a byte well past the header, inside frame ciphertext.
    blob[len(blob) // 2] ^= 0xFF
    with pytest.raises(SnapshotDecryptError):
        _decrypt(bytes(blob), "pw-123456")


def test_trailing_junk_rejected():
    blob = _encrypt(b"data", "pw-123456")
    with pytest.raises(SnapshotDecryptError, match="after the final marker"):
        _decrypt(blob + b"junk", "pw-123456")


def test_bad_magic_rejected():
    with pytest.raises(SnapshotDecryptError, match="magic"):
        open_decrypting_reader(io.BytesIO(b"not-a-snapshot-artifact"), "pw")


def test_empty_passphrase_rejected():
    from nymeria.core.snapshot_crypto import SnapshotCryptoError

    with pytest.raises(SnapshotCryptoError):
        _encrypt(b"data", "")


def test_sniff_artifact(tmp_path):
    encrypted = tmp_path / "artifact.nysnap"
    encrypted.write_bytes(_encrypt(b"data", "pw-123456"))
    assert sniff_artifact(encrypted) == "encrypted"

    import gzip

    plain = tmp_path / "artifact.tar.gz"
    plain.write_bytes(gzip.compress(b"data"))
    assert sniff_artifact(plain) == "plain"

    other = tmp_path / "other.bin"
    other.write_bytes(b"\x00\x01\x02")
    assert sniff_artifact(other) is None
    assert sniff_artifact(tmp_path / "missing.bin") is None
    assert not MAGIC.startswith(b"\x1f\x8b")
