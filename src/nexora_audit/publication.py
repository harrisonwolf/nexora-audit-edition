"""Validate in private staging, then publish through one directory rename."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import uuid
import stat
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
_COLLISION_ERRNOS = frozenset(
    {
        errno.EEXIST,
        errno.ENOTEMPTY,
        errno.EISDIR,
        errno.ENOTDIR,
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


def _ensure_directory_durable(path: Path) -> None:
    """Create each missing directory and synchronize its parent link."""
    missing: list[Path] = []
    current = Path(path)
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"directory path may not contain a symlink: {current}")
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ValueError(f"directory path has no existing ancestor: {path}")
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise ValueError(f"directory ancestor is not a real directory: {current}")

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"directory path may contain only real directories: {directory}")

    # Reconcile partial creation from an earlier failed attempt as well as this
    # call. Every directory link through the filesystem root is retried.
    current = Path(path)
    while True:
        if current.is_symlink() or not current.is_dir():
            raise ValueError(f"directory path may contain only real directories: {current}")
        _fsync_directory(current)
        parent = current.parent
        if parent == current:
            break
        current = parent


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


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ValueError(f"publication tree entry is not an unshared regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    records: list[tuple[str, str, int, str]] = []
    entries = (root, *sorted(root.rglob("*")))
    for entry in entries:
        status = entry.lstat()
        relative = "." if entry == root else entry.relative_to(root).as_posix()
        mode = stat.S_IMODE(status.st_mode)
        if stat.S_ISDIR(status.st_mode):
            records.append(("directory", relative, mode, ""))
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise ValueError(
                    f"publication tree entry is not an unshared regular file: {entry}"
                )
            content = entry.read_bytes()
            records.append(("file", relative, mode, hashlib.sha256(content).hexdigest()))
        else:
            raise ValueError(f"publication tree contains a symlink or special entry: {entry}")
    return tuple(records)


def _sync_tree(root: Path) -> None:
    directories = {root}
    root_status = root.lstat()
    if not stat.S_ISDIR(root_status.st_mode):
        raise ValueError(f"publication staging root is not a real directory: {root}")
    for entry in sorted(root.rglob("*")):
        status = entry.lstat()
        if stat.S_ISDIR(status.st_mode):
            directories.add(entry)
        elif stat.S_ISREG(status.st_mode):
            _fsync_regular_file(entry)
            directories.add(entry.parent)
        else:
            raise ValueError(f"publication tree contains a symlink or special entry: {entry}")
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


def _collision(publication_id: str, staging: Path) -> PublicationCollisionError:
    return PublicationCollisionError(
        f"publication {publication_id!r} is occupied and was not replaced; "
        f"the rejected staged tree remains at {staging}"
    )


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
    _ensure_directory_durable(root)
    symlink = _first_symlink_component(root)
    if symlink is not None:
        raise ValueError(f"publication root contains a symlink at {symlink}")
    staging_root = root / _STAGING_NAME
    if staging_root.is_symlink():
        raise ValueError(f"publication staging root may not be a symlink: {staging_root}")
    _ensure_directory_durable(staging_root)
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
    sealed_snapshot = _tree_snapshot(staging)
    validator(staging)
    if _tree_snapshot(staging) != sealed_snapshot:
        raise ValueError("validator modified the sealed publication tree")

    # The callback may have rewritten identical bytes. Synchronize the exact
    # validated snapshot again, then prove that synchronization saw no drift.
    _sync_tree(staging)
    if _tree_snapshot(staging) != sealed_snapshot:
        raise ValueError("publication tree changed while sealing validated bytes")

    final = root / publication_id
    if final.is_symlink() or final.exists():
        raise _collision(publication_id, staging)

    try:
        os.rename(staging, final)
    except OSError as exc:
        if exc.errno in _COLLISION_ERRNOS and (final.is_symlink() or final.exists()):
            raise _collision(publication_id, staging) from exc
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
