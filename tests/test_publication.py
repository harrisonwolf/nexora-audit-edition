from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import multiprocessing
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.manifest import verify_and_read
from nexora_audit.publication import (
    PublicationCollisionError,
    PublicationDurabilityError,
    discover_publications,
    publish_atomically,
)


def _files(value: int) -> dict[str, bytes]:
    payload = (json.dumps({"value": value}, sort_keys=True) + "\n").encode()
    manifest = {
        "schema_version": 1,
        "artifact_id": f"artifact-{value}",
        "producer_version": "v1",
        "payload": {
            "path": "payload.json",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    return {
        "payload.json": payload,
        "manifest.json": (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    }


def _validator(expected: int):
    def validate(staged: Path) -> None:
        verify_and_read(
            staged,
            expected_artifact_id=f"artifact-{expected}",
            supported_producer_versions={"v1"},
        )
    return validate


def _publish_in_process(
    root_text: str,
    publication_id: str,
    value: int,
    barrier,
    results,
) -> None:
    def validate(staged: Path) -> None:
        _validator(value)(staged)
        barrier.wait()

    try:
        publish_atomically(
            Path(root_text),
            publication_id,
            _files(value),
            validator=validate,
        )
        results.put(("published", value, ""))
    except PublicationCollisionError:
        results.put(("collision", value, ""))
    except Exception as exc:
        results.put(("error", value, f"{type(exc).__name__}: {exc}"))


def _publish_at_rename_checkpoint(
    root_text: str,
    timing: str,
    reached,
) -> None:
    from nexora_audit import publication

    real_rename = publication.os.rename

    def checkpoint_rename(source: Path, target: Path) -> None:
        if timing == "before":
            reached.set()
            signal.pause()
        real_rename(source, target)
        if timing == "after":
            reached.set()
            signal.pause()

    with patch.object(publication.os, "rename", side_effect=checkpoint_rename):
        publish_atomically(
            Path(root_text),
            "release-1",
            _files(1),
            validator=_validator(1),
        )


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temp.name) / "published"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_validates_before_the_final_name_becomes_discoverable(self) -> None:
        observed: list[tuple[Path, ...]] = []

        def validate(staged: Path) -> None:
            observed.append(discover_publications(self.root))
            _validator(1)(staged)

        final = publish_atomically(self.root, "release-1", _files(1), validator=validate)
        self.assertEqual(observed, [()])
        self.assertEqual(discover_publications(self.root), (final,))
        self.assertEqual((final / "payload.json").read_text(encoding="utf-8"), '{"value": 1}\n')

    def test_failed_validation_never_creates_a_discoverable_release(self) -> None:
        def reject(_staged: Path) -> None:
            raise ValueError("synthetic rejection")

        with self.assertRaisesRegex(ValueError, "synthetic rejection"):
            publish_atomically(self.root, "release-1", _files(1), validator=reject)
        self.assertEqual(discover_publications(self.root), ())
        self.assertFalse((self.root / "release-1").exists())

    def test_validator_cannot_modify_the_tree_that_will_be_published(self) -> None:
        def mutate(staged: Path) -> None:
            _validator(1)(staged)
            (staged / "payload.json").write_text('{"value": 2}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "validator modified"):
            publish_atomically(self.root, "release-1", _files(1), validator=mutate)

        self.assertEqual(discover_publications(self.root), ())
        self.assertFalse((self.root / "release-1").exists())


    def test_validator_same_byte_rewrite_is_resynchronized_before_publish(self) -> None:
        from nexora_audit import publication

        order: list[str] = []
        real_sync = publication._fsync_regular_file

        def record(path: Path) -> None:
            order.append(f"sync:{path.name}")
            real_sync(path)

        def rewrite_same_bytes(staged: Path) -> None:
            order.append("validator")
            payload = staged / "payload.json"
            payload.write_bytes(payload.read_bytes())

        with patch.object(publication, "_fsync_regular_file", side_effect=record):
            publish_atomically(
                self.root,
                "release-1",
                _files(1),
                validator=rewrite_same_bytes,
            )

        validator_index = order.index("validator")
        self.assertTrue(any(item.startswith("sync:") for item in order[validator_index + 1 :]))

    def test_validator_snapshot_binds_paths_types_modes_and_hardlinks(self) -> None:
        def add_path(staged: Path) -> None:
            (staged / "added.txt").write_text("added\n", encoding="utf-8")

        def change_type(staged: Path) -> None:
            payload = staged / "payload.json"
            payload.unlink()
            payload.mkdir()

        def change_mode(staged: Path) -> None:
            payload = staged / "payload.json"
            payload.chmod((payload.stat().st_mode & 0o777) ^ 0o100)

        def add_hardlink(staged: Path) -> None:
            (staged / "linked.json").hardlink_to(staged / "payload.json")

        for name, mutate in (
            ("added-path", add_path),
            ("changed-type", change_type),
            ("changed-mode", change_mode),
            ("hardlink", add_hardlink),
        ):
            with self.subTest(name=name):
                publication_id = f"release-{name}"

                def validate_then_mutate(staged: Path) -> None:
                    _validator(1)(staged)
                    mutate(staged)

                with self.assertRaises(ValueError):
                    publish_atomically(
                        self.root,
                        publication_id,
                        _files(1),
                        validator=validate_then_mutate,
                    )
                self.assertFalse((self.root / publication_id).exists())
    def test_collision_never_replaces_the_incumbent(self) -> None:
        first = publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))
        before = (first / "payload.json").read_bytes()
        with self.assertRaises(PublicationCollisionError):
            publish_atomically(self.root, "release-1", _files(2), validator=_validator(2))
        self.assertEqual((first / "payload.json").read_bytes(), before)

    def test_concurrent_same_id_publish_has_exactly_one_winner(self) -> None:
        def attempt(value: int) -> str:
            try:
                publish_atomically(self.root, "release-1", _files(value), validator=_validator(value))
                return "published"
            except PublicationCollisionError:
                return "collision"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(attempt, (1, 2)))
        self.assertEqual(outcomes, ["collision", "published"])
        self.assertEqual(len(discover_publications(self.root)), 1)

    def test_multiprocess_same_id_publish_has_exactly_one_winner(self) -> None:
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        values = tuple(range(1, 5))
        barrier = context.Barrier(len(values), timeout=20)
        processes = [
            context.Process(
                target=_publish_in_process,
                args=(
                    str(self.root),
                    "release-1",
                    value,
                    barrier,
                    results,
                ),
            )
            for value in values
        ]
        started = []

        try:
            for process in processes:
                process.start()
                started.append(process)
            outcomes = [results.get(timeout=30) for _value in values]
            for process in started:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in started:
                if process.is_alive():
                    process.terminate()
                process.join(5)
            results.close()
            results.join_thread()

        errors = [detail for status, _value, detail in outcomes if status == "error"]
        winners = [value for status, value, _detail in outcomes if status == "published"]
        collisions = [value for status, value, _detail in outcomes if status == "collision"]
        self.assertEqual(errors, [])
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(collisions), len(values) - 1)
        self.assertEqual(discover_publications(self.root), (self.root / "release-1",))
        payload = json.loads((self.root / "release-1" / "payload.json").read_text())
        self.assertEqual(payload, {"value": winners[0]})

    def test_process_death_at_visibility_boundary_is_all_or_nothing(self) -> None:
        context = multiprocessing.get_context("fork")

        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                root = self.root / timing
                reached = context.Event()
                process = context.Process(
                    target=_publish_at_rename_checkpoint,
                    args=(str(root), timing, reached),
                )

                try:
                    process.start()
                    self.assertTrue(reached.wait(20), "child did not reach rename checkpoint")
                    process.kill()
                    process.join(10)
                    self.assertEqual(process.exitcode, -signal.SIGKILL)
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)

                final = root / "release-1"
                if timing == "before":
                    self.assertFalse(final.exists())
                    self.assertEqual(discover_publications(root), ())
                    publish_atomically(root, "release-1", _files(1), validator=_validator(1))
                else:
                    self.assertEqual(discover_publications(root), (final,))
                    with self.assertRaises(PublicationCollisionError):
                        publish_atomically(root, "release-1", _files(2), validator=_validator(2))

                payload = verify_and_read(
                    final,
                    expected_artifact_id="artifact-1",
                    supported_producer_versions={"v1"},
                )
                self.assertEqual(payload.content, b'{"value": 1}\n')

    def test_rejects_payload_paths_that_escape_staging(self) -> None:
        files = _files(1)
        files["../escape"] = b"x"
        with self.assertRaisesRegex(ValueError, "relative"):
            publish_atomically(self.root, "release-1", files, validator=lambda _path: None)

    def test_writes_manifest_last(self) -> None:
        from nexora_audit import publication

        observed: list[str] = []
        real_write = publication._write_durable

        def record(path: Path, content: bytes) -> None:
            observed.append(path.name)
            real_write(path, content)

        with patch.object(publication, "_write_durable", side_effect=record):
            publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))
        self.assertEqual(observed[-1], "manifest.json")

    def test_discovery_excludes_incomplete_staging_and_symlinks(self) -> None:
        self.root.mkdir(parents=True)
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        (incomplete / "payload.json").write_text("{}", encoding="utf-8")
        staging = self.root / ".staging" / "orphan"
        staging.mkdir(parents=True)
        (staging / "manifest.json").write_text("{}", encoding="utf-8")
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(discover_publications(self.root), ())

    def test_rejects_invalid_identity_and_non_bytes_content(self) -> None:
        for identity in ("", ".staging", "../release", "/absolute"):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    publish_atomically(self.root, identity, _files(1), validator=_validator(1))
        files = _files(1)
        files["payload.json"] = "not bytes"  # type: ignore[assignment]
        with self.assertRaisesRegex(TypeError, "must be bytes"):
            publish_atomically(self.root, "release-1", files, validator=_validator(1))

    def test_rejects_a_symlinked_staging_root(self) -> None:
        self.root.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            (self.root / ".staging").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_new_publication_root_is_durably_linked_from_its_parent(self) -> None:
        from nexora_audit import publication

        root = self.root / "nested"
        observed: list[Path] = []
        real_sync = publication._fsync_directory

        def record(path: Path) -> None:
            observed.append(path)
            real_sync(path)

        with patch.object(publication, "_fsync_directory", side_effect=record):
            publish_atomically(root, "release-1", _files(1), validator=_validator(1))

        self.assertIn(self.root.parent, observed)
        self.assertIn(self.root, observed)

    def test_root_creation_retry_resynchronizes_prior_ancestor_links(self) -> None:
        from nexora_audit import publication

        root = self.root / "nested"
        real_sync = publication._fsync_directory
        failed = False

        def fail_once(path: Path) -> None:
            nonlocal failed
            if path == self.root.parent and not failed:
                failed = True
                raise OSError(errno.EIO, "synthetic ancestor sync failure")
            real_sync(path)

        with patch.object(publication, "_fsync_directory", side_effect=fail_once):
            with self.assertRaises(OSError):
                publish_atomically(root, "release-1", _files(1), validator=_validator(1))

        observed: list[Path] = []

        def record(path: Path) -> None:
            observed.append(path)
            real_sync(path)

        with patch.object(publication, "_fsync_directory", side_effect=record):
            publish_atomically(root, "release-1", _files(1), validator=_validator(1))

        self.assertIn(self.root.parent, observed)

    def test_fsyncs_both_rename_parents_after_publication(self) -> None:
        from nexora_audit import publication

        observed: list[Path] = []
        real_sync = publication._fsync_directory

        def record(path: Path) -> None:
            observed.append(path)
            real_sync(path)

        with patch.object(publication, "_fsync_directory", side_effect=record):
            publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))
        self.assertIn(self.root / ".staging", observed)
        self.assertIn(self.root, observed)

    def test_interruption_before_visibility_does_not_leave_a_final_name_tombstone(self) -> None:
        from nexora_audit import publication

        final = self.root / "release-1"
        observed_final_existence: list[bool] = []

        def interrupt(_source: Path, target: Path) -> None:
            self.assertEqual(target, final)
            observed_final_existence.append(final.exists())
            raise OSError("synthetic process death before visibility")

        with patch.object(publication.os, "rename", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "synthetic process death"):
                publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))

        self.assertEqual(observed_final_existence, [False])
        self.assertFalse(final.exists())

    def test_existing_empty_final_directory_is_never_replaced(self) -> None:
        incumbent = self.root / "release-1"
        incumbent.mkdir(parents=True)
        self.assertEqual(discover_publications(self.root), ())

        with self.assertRaises(PublicationCollisionError):
            publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))

        self.assertTrue(incumbent.is_dir())
        self.assertEqual(tuple(incumbent.iterdir()), ())
        self.assertEqual(discover_publications(self.root), ())

    def test_existing_symlink_is_never_replaced(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("keep", encoding="utf-8")
        self.root.mkdir()
        final = self.root / "release-1"
        final.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PublicationCollisionError):
            publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))

        self.assertTrue(final.is_symlink())
        self.assertEqual((outside / "sentinel").read_text(encoding="utf-8"), "keep")

    def test_visibility_followed_by_sync_failure_reports_an_indeterminate_outcome(self) -> None:
        from nexora_audit import publication

        real_sync = publication._fsync_directory

        def fail_root_sync(path: Path) -> None:
            if path == self.root and (self.root / "release-1").exists():
                raise OSError(errno.EIO, "synthetic root sync failure")
            real_sync(path)

        with patch.object(publication, "_fsync_directory", side_effect=fail_root_sync):
            with self.assertRaises(PublicationDurabilityError) as caught:
                publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))

        final = self.root / "release-1"
        self.assertEqual(caught.exception.published_path, final)
        self.assertEqual(discover_publications(self.root), (final,))

    def test_unrelated_rename_failure_is_not_mislabeled_as_a_collision(self) -> None:
        from nexora_audit import publication

        failure = OSError(errno.EIO, "synthetic rename I/O failure")
        with patch.object(publication.os, "rename", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                publish_atomically(self.root, "release-1", _files(1), validator=_validator(1))

        self.assertNotIsInstance(caught.exception, PublicationCollisionError)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertFalse((self.root / "release-1").exists())
