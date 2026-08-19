from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.manifest import verify_and_read
from nexora_audit.publication import PublicationCollisionError, discover_publications, publish_atomically


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


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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
