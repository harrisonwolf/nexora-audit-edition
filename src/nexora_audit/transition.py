"""The write-ahead recovery state machine for multi-target runtime swaps.

A runtime generation is not one file. It is the runtime DB, the thumbnail
tree, the photo tree, and -- separately -- the served web build. Installing a
new generation is therefore several renames, and an exception or a process
death between them leaves the runtime serving a mixture of two publications:
new DB with old photos, or an old DB whose thumbnails already moved on.

This module makes that transition journaled and recoverable. Every phase is
published through one atomic marker-write primitive before and after the
rename it describes, so the next runtime operation can always decide -- from
the marker plus the *actual* identities on disk, never from the assumption
that the last write followed its rename -- whether to finish forward or roll
back. There are exactly two terminal states: the complete old generation, or
the postchecked complete new generation.

Nothing here auto-repairs an unknown, corrupt, or impossible state. Those
fail closed and keep every byte of evidence for the operator.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .console import HeartbeatReporter
from .sqlite_state import sqlite_session


MARKER_SCHEMA_VERSION = 1
MARKER_SUFFIX = ".transition.json"
LOCK_FILE_NAME = ".nexora-transition.lock"

# The ordered targets of one runtime generation, and of one build tree.
RUNTIME_TARGETS: tuple[str, ...] = ("db", "thumbs", "photos")
BUILD_TARGETS: tuple[str, ...] = ("build",)
KIND_TARGETS: Mapping[str, tuple[str, ...]] = {"runtime": RUNTIME_TARGETS, "build": BUILD_TARGETS}

# The build tree's own internal publication marker, and the entries a
# committed tree must carry.
BUILD_PUBLICATION_FILE = "build.json"
BUILD_REQUIRED_ENTRIES: tuple[str, ...] = (
    "index.html",
    "app.js",
    "style.css",
    "share.html",
    "share.js",
    BUILD_PUBLICATION_FILE,
)

_MARKER_FIELDS = frozenset({"marker_schema_version", "kind", "scope_id", "publication_id", "phase", "targets"})
_TARGET_FIELDS = frozenset({"name", "live", "stage", "backup", "old_existed", "old_identity", "new_identity"})
_IDENTITY_KINDS = frozenset({"absent", "file", "dir"})
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

ABSENT: dict[str, Any] = {"kind": "absent"}

_DIGEST_CHUNK = 1 << 20
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class TransitionCorruptionError(RuntimeError):
    """An unknown, corrupt, or impossible transition state. Never auto-repaired."""


class TransitionPostcheckError(RuntimeError):
    """A landed new generation failed its postchecks."""


class TransitionBusyError(RuntimeError):
    """Another process already owns this transition's mutation lock."""


# --------------------------------------------------------------------------
# Paths, phases, identities
# --------------------------------------------------------------------------


def marker_path_for(root: Path, scope_id: str) -> Path:
    """The recovery marker for one scope's transition under `root`."""
    if not isinstance(scope_id, str) or not _SCOPE_ID.fullmatch(scope_id):
        raise ValueError(f"scope_id must be a contained identifier, got {scope_id!r}")
    return Path(root) / f"{scope_id}{MARKER_SUFFIX}"


def _first_symlink_component(path: Path) -> Path | None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def new_transition_token() -> str:
    """A per-transition token that makes every stage/backup path unique."""
    return uuid.uuid4().hex[:12]


def stage_and_backup_for(live: Path, token: str) -> tuple[Path, Path]:
    """Unique same-filesystem siblings of `live` for this transition."""
    return (
        live.with_name(f"{live.name}.{token}.stage"),
        live.with_name(f"{live.name}.{token}.backup"),
    )


def phase_sequence(target_names: Sequence[str]) -> tuple[str, ...]:
    """The only valid durable phases, in order, for these ordered targets."""
    phases: list[str] = ["prepared"]
    for name in target_names:
        phases.extend((f"intent_backup_{name}", f"backed_up_{name}"))
    for name in target_names:
        phases.extend((f"intent_install_{name}", f"installed_{name}"))
    phases.extend(("new_verified", "cleanup"))
    return tuple(phases)


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_DIGEST_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def identity_of(path: Path) -> dict[str, Any]:
    """A digest identity strong enough to tell an old target from a new one."""
    if path.is_symlink():
        raise TransitionCorruptionError(
            f"Transition target {path} is a symlink; runtime targets are real files or directories."
        )
    if not path.exists():
        return dict(ABSENT)
    if path.is_dir():
        digest = hashlib.sha256()
        entries = 0
        for entry in sorted(path.rglob("*")):
            relative = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                raise TransitionCorruptionError(
                    f"Transition target {path} contains a symlink at {relative}; refusing to journal it."
                )
            entry_mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(entry_mode):
                digest.update(f"D\0{relative}\0\n".encode("utf-8"))
                continue
            if not stat.S_ISREG(entry_mode):
                raise TransitionCorruptionError(
                    f"Transition target {path} contains a non-regular entry at {relative}."
                )
            size, file_digest = _file_digest(entry)
            entries += 1
            digest.update(f"F\0{relative}\0{size}\0{file_digest}\n".encode("utf-8"))
        return {"kind": "dir", "entries": entries, "digest": digest.hexdigest()}
    path_mode = path.stat(follow_symlinks=False).st_mode
    if stat.S_ISREG(path_mode):
        size, file_digest = _file_digest(path)
        return {"kind": "file", "size": size, "digest": file_digest}
    raise TransitionCorruptionError(f"Transition target {path} is neither a file nor a directory.")


# --------------------------------------------------------------------------
# The one atomic marker-write primitive
# --------------------------------------------------------------------------


def _serialize_marker(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True) + "\n"


_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EINVAL, errno.ENOTSUP if hasattr(errno, 'ENOTSUP') else errno.EOPNOTSUPP, errno.EOPNOTSUPP, errno.EROFS}
)


def _fsync_file(handle: Any) -> None:
    os.fsync(handle.fileno())


def _write_temp_marker(temp_path: Path, payload: str) -> None:
    handle = open(temp_path, "w", encoding="utf-8", newline="\n")
    try:
        handle.write(payload)
        handle.flush()
        _fsync_file(handle)
    finally:
        handle.close()


def _atomic_replace(source: Path, target: Path) -> None:
    source.replace(target)


def _fsync_dir(path: Path) -> None:
    # Directory fsync is the durability half of an atomic rename. Some
    # filesystems (notably 9p/DrvFS mounts) refuse it; the rename is still
    # atomic there, so a refusal must not fail the transition.
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - platform dependent
        if exc.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
            raise
        return
    try:
        os.fsync(handle)
    except OSError as exc:
        # Only a filesystem that cannot fsync a directory (9p/DrvFS) is
        # excused; a real I/O failure must not report durability it lacks.
        if exc.errno not in _FSYNC_UNSUPPORTED_ERRNOS:  # pragma: no cover - platform dependent
            raise
    finally:
        os.close(handle)


def _ensure_directory_durable(path: Path) -> None:
    """Create each missing directory and synchronize its parent link."""
    missing: list[Path] = []
    current = Path(path)
    while not current.exists():
        if current.is_symlink():
            raise TransitionCorruptionError(
                f"Transition root path may not contain a symlink: {current}."
            )
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise TransitionCorruptionError(
                f"Transition root path has no existing ancestor: {path}."
            )
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise TransitionCorruptionError(
            f"Transition root ancestor is not a real directory: {current}."
        )

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise TransitionCorruptionError(
                f"Transition root path may contain only real directories: {directory}."
            )

    # A failed earlier sync may have left created directories behind. Retrying
    # the full ancestry makes that partial state reconcilable.
    current = Path(path)
    while True:
        if current.is_symlink() or not current.is_dir():
            raise TransitionCorruptionError(
                f"Transition root path may contain only real directories: {current}."
            )
        _fsync_dir(current)
        parent = current.parent
        if parent == current:
            break
        current = parent


def _fsync_regular_file(path: Path) -> None:
    """Synchronize one non-symlink regular file without requiring write access."""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TransitionCorruptionError(
                f"Transition input {path} is not a regular file and cannot be synchronized."
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ancestor_chain(start: Path, root: Path) -> tuple[Path, ...]:
    """Return directory parents from start through the owned root."""
    chain: list[Path] = []
    current = start
    while True:
        chain.append(current)
        if current == root:
            return tuple(chain)
        if root not in current.parents:
            raise TransitionCorruptionError(
                f"Transition input parent {start} is outside the owned root {root}."
            )
        current = current.parent


def _synchronize_transition_target(marker_path: Path, path: Path) -> None:
    """Flush one closed target and its containing directory entries."""
    if path.is_symlink() or not path.exists():
        raise _fail(marker_path, f"transition input {path} disappeared or became a symlink")

    directories: set[Path] = set()
    path_mode = path.stat(follow_symlinks=False).st_mode
    if stat.S_ISREG(path_mode):
        _fsync_regular_file(path)
    elif stat.S_ISDIR(path_mode):
        directories.add(path)
        for entry in sorted(path.rglob("*")):
            if entry.is_symlink():
                raise _fail(marker_path, f"transition input {path} contains a symlink at {entry}")
            entry_mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(entry_mode):
                directories.add(entry)
            elif stat.S_ISREG(entry_mode):
                _fsync_regular_file(entry)
                directories.add(entry.parent)
            else:
                raise _fail(
                    marker_path,
                    f"transition input {path} contains a non-regular entry at {entry}",
                )
    else:
        raise _fail(marker_path, f"transition input {path} is not a regular file or directory")

    root = marker_path.parent
    directories.update(_ancestor_chain(path.parent, root))
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        _fsync_dir(directory)


def _synchronize_entry_targets(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Make incumbent and staged target contents durable before journal intent."""
    synchronized: set[Path] = set()
    for target in record["targets"]:
        for slot, identity_field in (("live", "old_identity"), ("stage", "new_identity")):
            path = Path(target[slot])
            if target[identity_field]["kind"] == "absent" or path in synchronized:
                continue
            _synchronize_transition_target(marker_path, path)
            synchronized.add(path)


def _fsync_marker_parent(marker_path: Path) -> None:
    _fsync_dir(marker_path.parent)


def _require_safe_marker_path(marker_path: Path) -> None:
    if not marker_path.is_absolute() or ".." in marker_path.parts:
        raise TransitionCorruptionError(
            f"Transition marker path must be canonical absolute without parent segments: {marker_path}"
        )
    symlink = _first_symlink_component(marker_path)
    if symlink is not None:
        raise TransitionCorruptionError(
            f"Transition marker path {marker_path} contains a symlink at {symlink}."
        )


@contextlib.contextmanager
def transition_lock(marker_path: Path) -> Iterable[None]:
    """Acquire the non-blocking process lock for the whole owned root."""
    marker_path = Path(marker_path)
    _require_safe_marker_path(marker_path)
    _ensure_directory_durable(marker_path.parent)
    _require_safe_marker_path(marker_path)
    lock_path = marker_path.parent / LOCK_FILE_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TransitionCorruptionError(f"Could not open transition lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransitionBusyError(f"Transition lock is already held: {lock_path}") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_marker_unlocked(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Publish a complete marker record atomically.

    Serialize, write and fsync a unique same-directory temporary file,
    atomically replace the live marker, then fsync the parent directory. The
    live marker is never truncated or rewritten in place, so a failure at any
    step leaves the visible marker as either the previous or the next complete
    record. A temporary residue may survive; it is never authoritative.
    """
    payload = _serialize_marker(record)
    _ensure_directory_durable(marker_path.parent)
    temp_path = marker_path.with_name(f"{marker_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_temp_marker(temp_path, payload)
        _atomic_replace(temp_path, marker_path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    _fsync_marker_parent(marker_path)


def write_marker(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Validate and publish an initial prepared marker without clobbering one."""
    with transition_lock(marker_path):
        current = _validated_entry_record(marker_path, record)
        _write_marker_unlocked(marker_path, current)


# --------------------------------------------------------------------------
# Marker reading and validation
# --------------------------------------------------------------------------


def _fail(marker_path: Path, message: str) -> TransitionCorruptionError:
    return TransitionCorruptionError(
        f"Runtime transition marker {marker_path} is not a state this build can resolve: {message}. "
        "Nothing was changed and every file was left in place. Inspect the marker, the "
        "*.stage/*.backup siblings, and the live targets, then recover by hand."
    )


def _require_contained(marker_path: Path, root: Path, candidate: Any, *, field: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise _fail(marker_path, f"target {field} is not a path")
    path = Path(candidate)
    if not path.is_absolute() or ".." in path.parts:
        raise _fail(marker_path, f"target {field} {candidate!r} is not a contained absolute path")
    root = root.absolute()
    root_symlink = _first_symlink_component(root)
    path_symlink = _first_symlink_component(path)
    if root_symlink is not None:
        raise _fail(marker_path, f"transition root contains a symlink at {root_symlink}")
    if path_symlink is not None:
        raise _fail(marker_path, f"target {field} contains a symlink at {path_symlink}")
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        raise _fail(marker_path, f"target {field} {candidate!r} escapes {resolved_root}") from None
    return path


def _marker_temp_name(name: str) -> bool:
    prefix, separator, tail = name.rpartition(f"{MARKER_SUFFIX}.")
    if not separator or not _SCOPE_ID.fullmatch(prefix):
        return False
    token, dot, extension = tail.partition(".")
    return (
        dot == "."
        and extension == "tmp"
        and len(token) == 32
        and all(character in "0123456789abcdef" for character in token)
    )


def _require_data_slot(
    marker_path: Path,
    root: Path,
    path: Path,
    *,
    field: str,
) -> None:
    relative = path.relative_to(root)
    if not relative.parts:
        raise _fail(marker_path, f"target {field} reuses the transition ownership root")
    control_name = relative.parts[0]
    if (
        control_name == LOCK_FILE_NAME
        or control_name.endswith(MARKER_SUFFIX)
        or _marker_temp_name(control_name)
    ):
        raise _fail(
            marker_path,
            f"target {field} enters reserved transition control namespace {control_name!r}",
        )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_identity(marker_path: Path, value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in _IDENTITY_KINDS:
        raise _fail(marker_path, f"{field} is not a known identity record")
    kind = value["kind"]
    expected_fields = {
        "absent": {"kind"},
        "file": {"kind", "size", "digest"},
        "dir": {"kind", "entries", "digest"},
    }[kind]
    if set(value) != expected_fields:
        raise _fail(marker_path, f"{field} has unexpected identity fields")
    if kind == "file":
        count = value["size"]
    elif kind == "dir":
        count = value["entries"]
    else:
        count = None
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise _fail(marker_path, f"{field} has an invalid identity count")
    if kind != "absent" and (
        not isinstance(value["digest"], str) or not _HEX_DIGEST.fullmatch(value["digest"])
    ):
        raise _fail(marker_path, f"{field} has an invalid identity digest")
    return dict(value)


def _validate_record(marker_path: Path, record: Any) -> dict[str, Any]:
    _require_safe_marker_path(marker_path)
    if not isinstance(record, dict):
        raise _fail(marker_path, "the record is not a JSON object")
    if set(record) != _MARKER_FIELDS:
        unexpected = sorted(set(record) - _MARKER_FIELDS)
        missing = sorted(_MARKER_FIELDS - set(record))
        raise _fail(marker_path, f"unexpected fields {unexpected}, missing fields {missing}")
    if (
        type(record["marker_schema_version"]) is not int
        or record["marker_schema_version"] != MARKER_SCHEMA_VERSION
    ):
        raise _fail(marker_path, f"marker schema version {record['marker_schema_version']!r} is unknown")
    kind = record["kind"]
    if kind not in KIND_TARGETS:
        raise _fail(marker_path, f"transition kind {kind!r} is unknown")
    all_slots: list[tuple[str, Path]] = []
    for field in ("scope_id", "publication_id"):
        if not isinstance(record[field], str) or not record[field]:
            raise _fail(marker_path, f"{field} is missing")
    if not _SCOPE_ID.fullmatch(record["scope_id"]):
        raise _fail(marker_path, f"scope_id {record['scope_id']!r} is not a contained identifier")
    expected_marker_name = f"{record['scope_id']}{MARKER_SUFFIX}"
    if marker_path.name != expected_marker_name:
        raise _fail(marker_path, f"marker name does not match scope_id {record['scope_id']!r}")

    targets = record["targets"]
    expected_names = KIND_TARGETS[kind]
    if not isinstance(targets, list) or len(targets) != len(expected_names):
        raise _fail(marker_path, f"a {kind} transition has exactly {len(expected_names)} targets")
    root = marker_path.parent
    for target, expected_name in zip(targets, expected_names):
        if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
            raise _fail(marker_path, "a target record has unexpected fields")
        if target["name"] != expected_name:
            raise _fail(marker_path, f"target {target['name']!r} is not {expected_name!r} in order")
        slots = {
            field: _require_contained(marker_path, root, target[field], field=f"{expected_name}.{field}")
            for field in ("live", "stage", "backup")
        }
        parents = {slot.parent for slot in slots.values()}
        if len(parents) != 1:
            raise _fail(marker_path, f"target {expected_name} live/stage/backup are not siblings")
        if len({str(slot) for slot in slots.values()}) != 3:
            raise _fail(marker_path, f"target {expected_name} reuses one path for two slots")
        for field, slot in slots.items():
            label = f"{expected_name}.{field}"
            _require_data_slot(marker_path, root, slot, field=label)
            for prior_label, prior_slot in all_slots:
                if slot == prior_slot:
                    raise _fail(marker_path, f"target {label} aliases {prior_label}")
                if _paths_overlap(slot, prior_slot):
                    raise _fail(
                        marker_path,
                        f"target {label} has an ancestor/descendant overlap with {prior_label}",
                    )
            all_slots.append((label, slot))
        old_identity = _validate_identity(marker_path, target["old_identity"], field=f"{expected_name}.old_identity")
        _validate_identity(marker_path, target["new_identity"], field=f"{expected_name}.new_identity")
        old_existed = target["old_existed"]
        if not isinstance(old_existed, bool):
            raise _fail(marker_path, f"target {expected_name} old_existed is not a boolean")
        if old_existed == (old_identity["kind"] == "absent"):
            raise _fail(
                marker_path,
                f"target {expected_name} claims old_existed={old_existed} with an "
                f"{old_identity['kind']} old identity",
            )

    valid_phases = (*phase_sequence(expected_names), "rollback")
    if record["phase"] not in valid_phases:
        raise _fail(marker_path, f"phase {record['phase']!r} is not a valid durable phase")
    return record


def read_marker(marker_path: Path) -> dict[str, Any] | None:
    """Return the validated visible marker, or None when there is none."""
    _require_safe_marker_path(marker_path)
    if not marker_path.exists():
        return None
    try:
        body = marker_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(marker_path, f"it could not be read ({exc})") from exc
    try:
        record = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail(marker_path, f"it is not readable JSON ({exc})") from exc
    return _validate_record(marker_path, record)


def _pending_marker_paths(marker_path: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            entry
            for entry in marker_path.parent.iterdir()
            if entry.name.endswith(MARKER_SUFFIX)
        )
    )


def require_no_pending_transition(marker_path: Path) -> None:
    """Refuse to mutate the owned root while any transition marker is present."""
    pending = _pending_marker_paths(marker_path)
    if not pending:
        return
    raise TransitionCorruptionError(
        f"A runtime transition marker is present at {pending[0]}, so the owned root is "
        "mid-transition and this command would mutate an unresolved generation. Resolve "
        "every recorded transition before starting another mutation; nothing was changed."
    )


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def plan_transition(
    *,
    kind: str,
    scope_id: str,
    publication_id: str,
    marker_path: Path,
    targets: Sequence[tuple[str, Path, Path, Path]],
) -> dict[str, Any]:
    """Record the identities this transition is allowed to move between."""
    if kind not in KIND_TARGETS:
        raise ValueError(f"Unknown transition kind {kind!r}.")
    _require_safe_marker_path(marker_path)
    planned: list[dict[str, Any]] = []
    for name, live, stage, backup in targets:
        old_identity = identity_of(live)
        planned.append(
            {
                "name": name,
                "live": str(live),
                "stage": str(stage),
                "backup": str(backup),
                "old_existed": old_identity["kind"] != "absent",
                "old_identity": old_identity,
                "new_identity": identity_of(stage),
            }
        )
    record = {
        "marker_schema_version": MARKER_SCHEMA_VERSION,
        "kind": kind,
        "scope_id": scope_id,
        "publication_id": publication_id,
        "phase": "prepared",
        "targets": planned,
    }
    return _validate_record(marker_path, record)


# --------------------------------------------------------------------------
# Expected slot states
# --------------------------------------------------------------------------


def _slot_state(record: Mapping[str, Any], phase: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Where each target's bytes must be at one *complete* phase."""
    names = [target["name"] for target in record["targets"]]
    phases = phase_sequence(names)
    index = phases.index(phase)
    state: dict[str, dict[str, dict[str, Any]]] = {}
    for target in record["targets"]:
        name = target["name"]
        backed_up = index >= phases.index(f"backed_up_{name}")
        installed = index >= phases.index(f"installed_{name}")
        old = target["old_identity"]
        new = target["new_identity"]
        if not backed_up:
            state[name] = {"live": old, "stage": new, "backup": dict(ABSENT)}
        elif not installed:
            state[name] = {"live": dict(ABSENT), "stage": new, "backup": old}
        else:
            state[name] = {"live": new, "stage": dict(ABSENT), "backup": old}
    return state


def _actual_state(record: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        target["name"]: {slot: identity_of(Path(target[slot])) for slot in ("live", "stage", "backup")}
        for target in record["targets"]
    }


def _check_precommit_phase_legality(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Require the exact state permitted by the recorded precommit phase."""
    phase = record["phase"]
    if phase == "rollback":
        return
    names = [target["name"] for target in record["targets"]]
    phases = phase_sequence(names)
    if phase.startswith("intent_"):
        index = phases.index(phase)
        legal = (_slot_state(record, phases[index - 1]), _slot_state(record, phases[index + 1]))
    else:
        legal = (_slot_state(record, phase),)
    actual = _actual_state(record)
    if actual not in legal:
        raise _fail(
            marker_path,
            f"phase {phase} does not permit the identities currently present on disk",
        )


def _check_rollback_state_legality(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Prove every target is a recorded rollback state before mutating any target."""
    actual = _actual_state(record)
    for target in record["targets"]:
        name = target["name"]
        old = target["old_identity"]
        new = target["new_identity"]
        absent = dict(ABSENT)
        if target["old_existed"]:
            legal = (
                {"live": old, "stage": new, "backup": absent},
                {"live": absent, "stage": new, "backup": old},
                {"live": new, "stage": absent, "backup": old},
            )
        else:
            legal = (
                {"live": absent, "stage": new, "backup": absent},
                {"live": new, "stage": absent, "backup": absent},
                {"live": absent, "stage": absent, "backup": absent},
            )
        if actual[name] not in legal:
            raise _fail(
                marker_path,
                f"rollback target {name} is not in any identity state recorded by this transition",
            )


# --------------------------------------------------------------------------
# Postchecks
# --------------------------------------------------------------------------


def _postcheck_db(record: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    """Reopen the landed runtime DB and refuse to report a false success.

    The install is the moment the runtime generation changes, so the landed
    file is checked through a fresh close-owning handle rather than trusted:
    it must be readable, carry exactly one runtime_manifest row, and name the
    publication we just installed.
    """
    runtime_db = Path(target["live"])
    publication_id = str(record["publication_id"])
    remedy = (
        "Build a complete staged generation and retry. If the failure repeats, inspect the "
        f"staged artifact for {publication_id} and whether the runtime directory sits on a "
        "filesystem that defers renames."
    )
    if not runtime_db.exists():
        raise TransitionPostcheckError(
            f"Runtime DB {runtime_db} is missing immediately after the atomic swap; "
            f"bootstrap did not install publication {publication_id}. {remedy}"
        )
    try:
        with sqlite_session(runtime_db) as conn:
            rows = conn.execute("SELECT publication_id FROM runtime_manifest").fetchall()
    except sqlite3.DatabaseError as exc:
        raise TransitionPostcheckError(
            f"Runtime DB {runtime_db} is unreadable immediately after the atomic swap "
            f"({exc}); bootstrap did not install publication {publication_id}. {remedy}"
        ) from exc
    if len(rows) != 1:
        raise TransitionPostcheckError(
            f"Runtime DB {runtime_db} landed with {len(rows)} runtime_manifest row(s); "
            f"exactly 1 is required for publication {publication_id}. {remedy}"
        )
    landed_publication_id = rows[0]["publication_id"]
    if str(landed_publication_id) != publication_id:
        raise TransitionPostcheckError(
            f"Runtime DB {runtime_db} landed publication {str(landed_publication_id)!r} "
            f"but bootstrap installed {publication_id!r}. {remedy}"
        )


def _postcheck_identity(record: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    live = Path(target["live"])
    landed = identity_of(live)
    if landed != target["new_identity"]:
        raise TransitionPostcheckError(
            f"Runtime target {target['name']} at {live} does not match the generation this "
            f"transition installed for publication {record['publication_id']}: expected "
            f"{target['new_identity']}, found {landed}."
        )


def _postcheck_build(record: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    _postcheck_identity(record, target)
    build_dir = Path(target["live"])
    for entry in BUILD_REQUIRED_ENTRIES:
        if not (build_dir / entry).is_file():
            raise TransitionPostcheckError(
                f"Web build {build_dir} landed without {entry}; rebuild the complete staged tree."
            )
    marker = build_dir / BUILD_PUBLICATION_FILE
    try:
        internal = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionPostcheckError(
            f"Web build publication marker {marker} is unreadable ({exc}); rebuild the staged tree."
        ) from exc
    if not isinstance(internal, dict):
        raise TransitionPostcheckError(
            f"Web build publication marker {marker} must contain one JSON object; rebuild the staged tree."
        )
    if internal.get("publication_id") != record["publication_id"]:
        raise TransitionPostcheckError(
            f"Web build {build_dir} names publication {internal.get('publication_id')!r} but the "
            f"transition installed {record['publication_id']!r}; rebuild the staged tree."
        )


def _verify_new_generation(record: Mapping[str, Any]) -> None:
    for target in record["targets"]:
        if record["kind"] == "build":
            _postcheck_build(record, target)
        elif target["name"] == "db":
            _postcheck_identity(record, target)
            _postcheck_db(record, target)
        else:
            _postcheck_identity(record, target)


# --------------------------------------------------------------------------
# Executing and recovering
# --------------------------------------------------------------------------


def _rename(source: Path, target: Path) -> None:
    source = Path(source)
    target = Path(target)
    source.replace(target)
    for parent in sorted({source.parent, target.parent}):
        _fsync_dir(parent)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _publish(marker_path: Path, record: dict[str, Any], phase: str, reporter: HeartbeatReporter | None) -> None:
    record["phase"] = phase
    _write_marker_unlocked(marker_path, record)
    if reporter is not None:
        reporter.poll()


def _validated_entry_record(
    marker_path: Path,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a fresh prepared plan before writing a marker or moving a byte."""
    try:
        current = json.loads(json.dumps(dict(record)))
    except (TypeError, ValueError) as exc:
        raise _fail(marker_path, f"the entry record is not JSON serializable ({exc})") from exc
    current = _validate_record(marker_path, current)
    if current["phase"] != "prepared":
        raise _fail(marker_path, f"entry phase {current['phase']!r} is not 'prepared'")
    require_no_pending_transition(marker_path)
    _check_precommit_phase_legality(marker_path, current)
    _synchronize_entry_targets(marker_path, current)
    _check_precommit_phase_legality(marker_path, current)
    return current


def _execute_transition_unlocked(
    marker_path: Path,
    current: dict[str, Any],
    *,
    reporter: HeartbeatReporter | None = None,
) -> None:
    """Install one validated staged generation, journaling every durable phase."""
    targets = current["targets"]
    _publish(marker_path, current, "prepared", reporter)

    for target in targets:
        name = target["name"]
        _publish(marker_path, current, f"intent_backup_{name}", reporter)
        if target["old_existed"]:
            _rename(Path(target["live"]), Path(target["backup"]))
        _publish(marker_path, current, f"backed_up_{name}", reporter)

    for target in targets:
        name = target["name"]
        _publish(marker_path, current, f"intent_install_{name}", reporter)
        if target["new_identity"]["kind"] != "absent":
            _rename(Path(target["stage"]), Path(target["live"]))
        _publish(marker_path, current, f"installed_{name}", reporter)

    _verify_new_generation(current)
    # The sole runtime commit point: every landed postcheck has passed.
    _publish(marker_path, current, "new_verified", reporter)
    _publish(marker_path, current, "cleanup", reporter)
    _delete_residue(marker_path, current)


def _run_transition_unlocked(
    marker_path: Path,
    record: Mapping[str, Any],
    *,
    reporter: HeartbeatReporter | None = None,
) -> None:
    current = _validated_entry_record(marker_path, record)
    _execute_transition_unlocked(marker_path, current, reporter=reporter)


def run_transition(
    marker_path: Path,
    record: Mapping[str, Any],
    *,
    reporter: HeartbeatReporter | None = None,
) -> None:
    """Lock and install the staged generation."""
    with transition_lock(marker_path):
        _run_transition_unlocked(marker_path, record, reporter=reporter)


def run_transition_with_recovery(
    marker_path: Path,
    record: Mapping[str, Any],
    *,
    reporter: HeartbeatReporter | None = None,
) -> None:
    """Run the transition; on any failure resolve back to a terminal state."""
    with transition_lock(marker_path):
        current = _validated_entry_record(marker_path, record)
        try:
            _execute_transition_unlocked(marker_path, current, reporter=reporter)
        except BaseException as original:
            try:
                _resolve_pending_transition_unlocked(marker_path, reporter=reporter)
            except BaseException as recovery_failure:
                raise recovery_failure from original
            raise


def _delete_residue(marker_path: Path, record: Mapping[str, Any]) -> None:
    parents: set[Path] = set()
    for target in record["targets"]:
        for slot in ("stage", "backup"):
            path = Path(target[slot])
            _remove(path)
            parents.add(path.parent)
    for parent in sorted(parents):
        if parent.exists():
            _fsync_dir(parent)
    # The marker is deleted last: while it exists, recovery is still possible.
    with contextlib.suppress(FileNotFoundError):
        marker_path.unlink()
    _fsync_dir(marker_path.parent)


def _roll_back_to_old(marker_path: Path, record: Mapping[str, Any]) -> None:
    """Put the old generation back, target by target, in reverse order."""
    for target in reversed(list(record["targets"])):
        name = target["name"]
        live = Path(target["live"])
        stage = Path(target["stage"])
        backup = Path(target["backup"])
        if not target["old_existed"]:
            if backup.exists():
                raise _fail(marker_path, f"target {name} has a backup although nothing existed to back up")
            # Enforce absence: whatever sits at live is the discarded new
            # generation or residue of one.
            _remove(live)
            continue
        if backup.exists():
            if live.exists():
                # Move the installed new target aside when its stage slot is
                # free, so the evidence survives until cleanup.
                if stage.exists():
                    _remove(live)
                else:
                    _rename(live, stage)
            _rename(backup, live)
            continue
        # No backup: the backup rename never completed, so the old generation
        # must still be at live. Anything else is corruption.
        if identity_of(live) != target["old_identity"]:
            raise _fail(
                marker_path,
                f"target {name} has neither its recorded backup nor its recorded old generation "
                f"at {live}",
            )


def _verify_old_generation(marker_path: Path, record: Mapping[str, Any]) -> None:
    for target in record["targets"]:
        live = Path(target["live"])
        landed = identity_of(live)
        if landed != target["old_identity"]:
            raise _fail(
                marker_path,
                f"target {target['name']} did not return to its recorded old generation at {live}",
            )


def _resolve_pending_transition_unlocked(
    marker_path: Path,
    *,
    reporter: HeartbeatReporter | None = None,
) -> str | None:
    """Idempotently finish or undo whatever the marker describes.

    Returns None when there was nothing to do, `"kept_new"` when the new
    generation was already committed and still passes its postchecks, or
    `"restored_old"` when the old generation was put back.
    """
    record = read_marker(marker_path)
    if record is None:
        return None

    phase = record["phase"]
    if reporter is not None:
        reporter.step(
            "runtime-transition",
            f"resolving an interrupted {record['kind']} transition to "
            f"{record['publication_id']} (phase {phase})",
        )

    if phase in ("new_verified", "cleanup"):
        # Past the commit point: never guess, never partially restore.
        _verify_new_generation(record)
        if phase != "cleanup":
            _publish(marker_path, record, "cleanup", reporter)
        _delete_residue(marker_path, record)
        return "kept_new"

    _check_precommit_phase_legality(marker_path, record)
    # Rollback has its own durable phase. It is safe to re-enter after a
    # second interruption but never weakens the initial phase check.
    if phase != "rollback":
        _publish(marker_path, record, "rollback", reporter)
    _check_rollback_state_legality(marker_path, record)
    _roll_back_to_old(marker_path, record)
    _verify_old_generation(marker_path, record)
    _delete_residue(marker_path, record)
    return "restored_old"


def resolve_pending_transition(
    marker_path: Path,
    *,
    reporter: HeartbeatReporter | None = None,
) -> str | None:
    """Lock and resolve one pending transition."""
    with transition_lock(marker_path):
        return _resolve_pending_transition_unlocked(marker_path, reporter=reporter)


def _validated_sweep_targets(
    marker_path: Path,
    live_targets: Sequence[Path],
) -> tuple[Path, ...]:
    root = marker_path.parent
    validated: list[Path] = []
    seen: set[str] = set()
    for index, candidate in enumerate(live_targets):
        live = _require_contained(
            marker_path,
            root,
            str(Path(candidate)),
            field=f"live_targets[{index}]",
        )
        identity = str(live)
        if identity in seen:
            raise _fail(marker_path, f"live_targets[{index}] duplicates {identity}")
        seen.add(identity)
        validated.append(live)
    return tuple(validated)


def _sweep_orphaned_stage_paths_unlocked(marker_path: Path, live_targets: Sequence[Path]) -> None:
    """Drop contained orphan residue only when the entire owned root is idle."""
    validated = _validated_sweep_targets(marker_path, live_targets)
    if _pending_marker_paths(marker_path):
        return
    root = marker_path.parent
    candidates: dict[str, Path] = {}
    for live in validated:
        parent = live.parent
        if not parent.is_dir():
            continue
        prefix = f"{live.name}."
        for sibling in sorted(parent.iterdir()):
            if not sibling.name.startswith(prefix):
                continue
            if not sibling.name.endswith((".stage", ".backup")):
                continue
            contained = _require_contained(
                marker_path,
                root,
                str(sibling),
                field=f"orphan sibling of {live.name}",
            )
            candidates[str(contained)] = contained
    for candidate in candidates.values():
        _remove(candidate)


def sweep_orphaned_stage_paths(marker_path: Path, live_targets: Sequence[Path]) -> None:
    """Lock and remove unjournaled stage/backup residue."""
    with transition_lock(marker_path):
        _sweep_orphaned_stage_paths_unlocked(marker_path, live_targets)


def resolve_all_pending_transitions(
    marker_paths: Iterable[Path],
    *,
    reporter: HeartbeatReporter | None = None,
) -> None:
    for marker_path in marker_paths:
        resolve_pending_transition(marker_path, reporter=reporter)
