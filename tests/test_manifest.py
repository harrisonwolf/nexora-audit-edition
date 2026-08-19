from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.manifest import ManifestIntegrityError, verify_and_read


class ManifestIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bundle(
        self,
        *,
        artifact_id: str = "artifact-1",
        producer_version: str = "v1",
        payload_name: str = "payload.json",
        payload: bytes = b'{"value": 7}\n',
    ) -> tuple[Path, dict[str, object]]:
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / payload_name).write_bytes(payload)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "producer_version": producer_version,
            "payload": {
                "path": payload_name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return bundle, manifest

    def test_returns_the_exact_bytes_that_were_hashed(self) -> None:
        bundle, _ = self._bundle()
        verified = verify_and_read(
            bundle,
            expected_artifact_id="artifact-1",
            supported_producer_versions={"v1"},
        )
        self.assertEqual(verified.content, b'{"value": 7}\n')
        self.assertEqual(verified.size_bytes, len(verified.content))
        self.assertEqual(verified.sha256, hashlib.sha256(verified.content).hexdigest())

    def test_rejects_path_traversal_before_reading(self) -> None:
        bundle, manifest = self._bundle()
        manifest["payload"]["path"] = "../outside.json"  # type: ignore[index]
        (self.root / "outside.json").write_text("private", encoding="utf-8")
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ManifestIntegrityError, "contained"):
            verify_and_read(
                bundle,
                expected_artifact_id="artifact-1",
                supported_producer_versions={"v1"},
            )

    def test_rejects_symlink_payload(self) -> None:
        bundle, manifest = self._bundle()
        outside = self.root / "outside.json"
        outside.write_text("private", encoding="utf-8")
        (bundle / "payload.json").unlink()
        (bundle / "payload.json").symlink_to(outside)
        manifest["payload"] = {
            "path": "payload.json",
            "size_bytes": outside.stat().st_size,
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ManifestIntegrityError, "symlink"):
            verify_and_read(
                bundle,
                expected_artifact_id="artifact-1",
                supported_producer_versions={"v1"},
            )

    def test_rejects_symlink_manifest(self) -> None:
        bundle, _ = self._bundle()
        outside = self.root / "outside-manifest.json"
        outside.write_text("{}", encoding="utf-8")
        (bundle / "manifest.json").unlink()
        (bundle / "manifest.json").symlink_to(outside)
        with self.assertRaisesRegex(ManifestIntegrityError, "manifest is a symlink"):
            verify_and_read(
                bundle,
                expected_artifact_id="artifact-1",
                supported_producer_versions={"v1"},
            )

    def test_rejects_malformed_schema_and_payload_declarations(self) -> None:
        cases = (
            ("schema_version", 2, "schema_version"),
            ("artifact_id", "", "non-empty"),
            ("producer_version", None, "non-empty"),
            ("payload", None, "must be an object"),
            ("schema_version", True, "schema_version"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                bundle, manifest = self._bundle()
                manifest[field] = value
                (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ManifestIntegrityError, message):
                    verify_and_read(
                        bundle,
                        expected_artifact_id="artifact-1",
                        supported_producer_versions={"v1"},
                    )
                for child in bundle.iterdir():
                    child.unlink()
                bundle.rmdir()

    def test_rejects_digest_size_identity_and_version_mismatches(self) -> None:
        cases = (("sha256", "0" * 64, "digest"), ("size_bytes", 1, "size"))
        for field, value, message in cases:
            with self.subTest(field=field):
                bundle, manifest = self._bundle()
                manifest["payload"][field] = value  # type: ignore[index]
                (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ManifestIntegrityError, message):
                    verify_and_read(
                        bundle,
                        expected_artifact_id="artifact-1",
                        supported_producer_versions={"v1"},
                    )
                for child in bundle.iterdir():
                    child.unlink()
                bundle.rmdir()

        bundle, _ = self._bundle()
        with self.assertRaisesRegex(ManifestIntegrityError, "artifact_id"):
            verify_and_read(bundle, expected_artifact_id="other", supported_producer_versions={"v1"})
        with self.assertRaisesRegex(ManifestIntegrityError, "producer_version"):
            verify_and_read(bundle, expected_artifact_id="artifact-1", supported_producer_versions={"v2"})
