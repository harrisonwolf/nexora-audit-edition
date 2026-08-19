from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nexora_audit

EXPECTED_CLAIMS = {
    "manifest-bound-integrity",
    "atomic-publication",
    "journaled-transition",
    "durable-sqlite-state",
    "qualified-composition",
}


class PublicClaimTests(unittest.TestCase):
    def test_every_public_claim_names_existing_executable_evidence(self) -> None:
        claims = json.loads((ROOT / "CLAIMS.json").read_text(encoding="utf-8"))
        self.assertIs(type(claims["schema_version"]), int)
        self.assertEqual(claims["schema_version"], 1)
        by_id = {claim["id"]: claim for claim in claims["claims"]}
        self.assertEqual(set(by_id), EXPECTED_CLAIMS)

        for claim in by_id.values():
            self.assertIsInstance(claim["statement"], str)
            self.assertTrue(claim["statement"].strip())
            self.assertTrue(claim["limitations"])
            self.assertTrue(claim["evidence"])
            for selector in claim["evidence"]:
                path_text, test_name = selector.split("::", 1)
                path = ROOT / path_text
                self.assertTrue(path.is_file(), selector)
                source = path.read_text(encoding="utf-8")
                method = test_name.rsplit(".", 1)[-1]

                self.assertIn(f"def {method}(", source, selector)

    def test_release_version_is_one_consistent_public_identity(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        claims = json.loads((ROOT / "CLAIMS.json").read_text(encoding="utf-8"))

        self.assertEqual(nexora_audit.__version__, "0.1.1")
        self.assertEqual(project["project"]["version"], nexora_audit.__version__)
        self.assertEqual(claims["scope"], f"Nexora Audit Edition {nexora_audit.__version__}")


if __name__ == "__main__":
    unittest.main()
