"""Manifest-bound artifact verification.

The consumer receives the same bytes that were hashed. Identity, producer
version, lexical containment, file type, byte length, and SHA-256 are checked
before those bytes are returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, NoReturn


_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


class ManifestIntegrityError(RuntimeError):
    """A manifest-bound artifact failed a trust-boundary check."""


@dataclass(frozen=True)
class VerifiedArtifact:
    artifact_id: str
    producer_version: str
    manifest_path: Path
    payload_path: Path
    sha256: str
    size_bytes: int
    content: bytes


def _fail(problem: str) -> NoReturn:
    raise ManifestIntegrityError(problem)


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"manifest field {field!r} must be a non-empty string")
    return value


def _contained_payload(bundle_dir: Path, declared: object) -> Path:
    relative = _require_nonempty_string(declared, "payload.path")
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.anchor or any(part in ("", ".", "..") for part in candidate.parts):
        _fail(f"payload path {relative!r} is not a contained relative path")
    if bundle_dir.is_symlink():
        _fail(f"bundle directory {bundle_dir} is a symlink")
    current = bundle_dir
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            _fail(f"payload path contains a symlink at {current}")
    try:
        common = os.path.commonpath((str(bundle_dir.resolve()), str(current.resolve(strict=False))))
    except ValueError:
        _fail(f"payload path {relative!r} is not contained by {bundle_dir}")
    if common != str(bundle_dir.resolve()):
        _fail(f"payload path {relative!r} is not contained by {bundle_dir}")
    return current


def _read_regular_file_once(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} is not a readable non-symlink file at {path}: {exc}")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"{label} is not a regular file at {path}")
        if metadata.st_size > max_bytes:
            _fail(
                f"{label} at {path} exceeds the {max_bytes}-byte maximum "
                f"({metadata.st_size} bytes observed)"
            )
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(_HASH_CHUNK_BYTES, max_bytes - observed + 1))
            if not chunk:
                break
            observed += len(chunk)
            if observed > max_bytes:
                _fail(f"{label} at {path} grew beyond the {max_bytes}-byte maximum while reading")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        _fail(f"{label} at {path} could not be read: {exc}")
    finally:
        os.close(descriptor)


def verify_and_read(
    bundle_dir: Path,
    *,
    expected_artifact_id: str,
    supported_producer_versions: Collection[str],
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> VerifiedArtifact:
    """Verify one bundle and return the exact payload bytes that were hashed."""
    if type(max_payload_bytes) is not int or max_payload_bytes <= 0:
        raise ValueError("max_payload_bytes must be a positive integer")
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        _fail(f"bundle directory does not exist: {bundle_dir}")
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_symlink():
        _fail(f"manifest is a symlink at {manifest_path}")
    try:
        manifest_bytes = _read_regular_file_once(
            manifest_path,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="manifest",
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"manifest is not readable JSON at {manifest_path}: {exc}")
    if not isinstance(manifest, dict):
        _fail("manifest must be a JSON object")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        _fail(f"unsupported manifest schema_version {schema_version!r}")

    artifact_id = _require_nonempty_string(manifest.get("artifact_id"), "artifact_id")
    if artifact_id != expected_artifact_id:
        _fail(f"artifact_id {artifact_id!r} does not match expected {expected_artifact_id!r}")
    producer_version = _require_nonempty_string(manifest.get("producer_version"), "producer_version")
    if producer_version not in supported_producer_versions:
        _fail(
            f"producer_version {producer_version!r} is not supported; "
            f"expected one of {sorted(supported_producer_versions)!r}"
        )

    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        _fail("manifest field 'payload' must be an object")
    payload_path = _contained_payload(bundle_dir, payload.get("path"))

    declared_size = payload.get("size_bytes")
    if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
        _fail(f"payload size declaration is malformed: {declared_size!r}")
    if declared_size > max_payload_bytes:
        _fail(
            f"payload size {declared_size} exceeds the configured "
            f"{max_payload_bytes}-byte maximum"
        )
    declared_digest = payload.get("sha256")
    if (
        not isinstance(declared_digest, str)
        or len(declared_digest) != 64
        or any(character not in "0123456789abcdef" for character in declared_digest)
    ):
        _fail(f"payload digest declaration is malformed: {declared_digest!r}")

    content = _read_regular_file_once(
        payload_path,
        max_bytes=max_payload_bytes,
        label="payload",
    )
    observed_size = len(content)
    if observed_size != declared_size:
        _fail(f"payload size mismatch: declared {declared_size}, read {observed_size}")
    observed_digest = hashlib.sha256(content).hexdigest()
    if observed_digest != declared_digest:
        _fail(f"payload digest mismatch: declared {declared_digest}, observed {observed_digest}")

    return VerifiedArtifact(
        artifact_id=artifact_id,
        producer_version=producer_version,
        manifest_path=manifest_path,
        payload_path=payload_path,
        sha256=observed_digest,
        size_bytes=observed_size,
        content=content,
    )
