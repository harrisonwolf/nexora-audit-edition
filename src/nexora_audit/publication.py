"""Validate in private staging, then publish through one directory rename."""

from __future__ import annotations

import errno
import os
import re
import uuid
from pathlib import Path
from typing import Callable, Mapping


_STAGING_NAME = ".staging"
_PUBLICATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FSYNC_UNSUPPORTED = frozenset(
    {
        errno.EINVAL,
        errno.EOPNOTSUPP,
        errno.ENOTSUP if hasattr(errno, "ENOTSUP") else errno.EOPNOTSUPP,
        errno.EROFS,
    }
)


class PublicationCollisionError(FileExistsError):
    """The requested final publication identity is already occupied."""


class PublicationDurabilityError(OSError):
    """Publication became visible, but a parent-directory fsync failed."""

    def __init__(self, published_path: Path, cause: OSError) -> None:
        super().__init__(
            f"publication is visible at {published_path}, but its directory sync failed: {cause}"
        )
        self.published_path = published_path


def _first_symlink_component(path: Path) -> Path | None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(descriptor)


def _relative_file(path: str) -> Path:
    relative = Path(path)
    if (
        not path
        or relative.is_absolute()
        or relative.anchor
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError(f"publication payload path must be a contained relative file path: {path!r}")
    return relative


def _write_durable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_tree(root: Path) -> None:
    directories = {root}
    for entry in root.rglob("*"):
        if entry.is_dir():
            directories.add(entry)
        else:
            directories.add(entry.parent)
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        _fsync_directory(directory)


def discover_publications(root: Path) -> tuple[Path, ...]:
    """Return only complete final directories carrying a regular manifest."""
    root = Path(root)
    if not root.is_dir():
        return ()
    discovered: list[Path] = []
    for candidate in sorted(root.iterdir()):
        if candidate.name == _STAGING_NAME or candidate.is_symlink() or not candidate.is_dir():
            continue
        manifest = candidate / "manifest.json"
        if manifest.is_file() and not manifest.is_symlink():
            discovered.append(candidate)
    return tuple(discovered)


def publish_atomically(
    root: Path,
    publication_id: str,
    files: Mapping[str, bytes],
    *,
    validator: Callable[[Path], None],
) -> Path:
    """Write and validate a complete staged tree before exposing its final name."""
    if not _PUBLICATION_ID.fullmatch(publication_id) or publication_id == _STAGING_NAME:
        raise ValueError(f"invalid publication_id {publication_id!r}")
    if "manifest.json" not in files:
        raise ValueError("publication must include manifest.json")

    root = Path(root).absolute()
    symlink = _first_symlink_component(root)
    if symlink is not None:
        raise ValueError(f"publication root contains a symlink at {symlink}")
    root.mkdir(parents=True, exist_ok=True)
    symlink = _first_symlink_component(root)
    if symlink is not None:
        raise ValueError(f"publication root contains a symlink at {symlink}")
    staging_root = root / _STAGING_NAME
    if staging_root.is_symlink():
        raise ValueError(f"publication staging root may not be a symlink: {staging_root}")
    staging_root.mkdir(exist_ok=True)
    if not staging_root.is_dir():
        raise ValueError(f"publication staging root is not a directory: {staging_root}")
    staging = staging_root / uuid.uuid4().hex
    staging.mkdir(exist_ok=False)

    normalized: dict[Path, bytes] = {}
    for name, content in files.items():
        relative = _relative_file(name)
        if relative in normalized:
            raise ValueError(f"duplicate normalized publication path: {name!r}")
        if not isinstance(content, bytes):
            raise TypeError(f"publication payload {name!r} must be bytes")
        normalized[relative] = content

    # A manifest-looking tree is still private until every payload is present.
    for relative in sorted(path for path in normalized if path.as_posix() != "manifest.json"):
        _write_durable(staging / relative, normalized[relative])
    _write_durable(staging / "manifest.json", normalized[Path("manifest.json")])
    _sync_tree(staging)
    validator(staging)

    final = root / publication_id
    try:
        final.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise PublicationCollisionError(
            f"publication {publication_id!r} already exists and was not replaced; "
            f"the rejected staged tree remains at {staging}"
        ) from exc
    try:
        os.rename(staging, final)
    except BaseException:
        try:
            final.rmdir()
        except OSError:
            pass
        raise
    sync_failure: OSError | None = None
    for parent in (staging_root, root):
        try:
            _fsync_directory(parent)
        except OSError as exc:
            if sync_failure is None:
                sync_failure = exc
    if sync_failure is not None:
        raise PublicationDurabilityError(final, sync_failure) from sync_failure
    return final
