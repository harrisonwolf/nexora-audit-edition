from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.sqlite_state import (
    create_runtime_database,
    insert_agent_token,
    insert_saved_report,
    read_durable_state,
    restore_durable_state,
    rebuild_runtime_database,
    sqlite_session,
)


class DurableSqliteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rebuild_preserves_credentials_reports_ownership_and_quota_history(self) -> None:
        old = self.root / "old.db"
        new = self.root / "new.db"
        create_runtime_database(old, publication_id="old-publication")
        insert_agent_token(
            old,
            token_hash="hash-1",
            agent_id="agent-1",
            label="primary",
            created_at="2026-08-18T00:00:00+00:00",
            quota_override=17,
        )
        insert_saved_report(
            old,
            report_id="report-1",
            agent_id="agent-1",
            created_at="2026-08-18T01:00:00+00:00",
            payload_json='{"synthetic": true}',
        )
        rebuild_runtime_database(old, new, publication_id="new-publication")
        state = read_durable_state(new)
        self.assertEqual(len(state["agent_tokens"]), 1)
        self.assertEqual(len(state["saved_reports"]), 1)
        self.assertEqual(state["agent_tokens"][0]["quota_override"], 17)
        self.assertEqual(state["saved_reports"][0]["agent_id"], "agent-1")
        with sqlite_session(new) as conn:
            publication = conn.execute("SELECT publication_id FROM runtime_manifest").fetchone()["publication_id"]
        self.assertEqual(publication, "new-publication")

    def test_legacy_database_without_durable_tables_reads_as_empty(self) -> None:
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        self.assertEqual(read_durable_state(legacy), {"agent_tokens": [], "saved_reports": []})

    def test_unreadable_database_fails_instead_of_silently_dropping_state(self) -> None:
        broken = self.root / "broken.db"
        broken.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            read_durable_state(broken)

    def test_strict_restore_rejects_duplicate_durable_rows(self) -> None:
        old = self.root / "old.db"
        new = self.root / "new.db"
        create_runtime_database(old, publication_id="old")
        insert_agent_token(
            old,
            token_hash="same",
            agent_id="agent-1",
            label=None,
            created_at="2026-08-18T00:00:00+00:00",
        )
        create_runtime_database(new, publication_id="new")
        insert_agent_token(
            new,
            token_hash="same",
            agent_id="agent-1",
            label=None,
            created_at="2026-08-18T00:00:00+00:00",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            restore_durable_state(new, read_durable_state(old))

    def test_session_closes_the_handle_on_exit(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        with sqlite_session(db) as conn:
            conn.execute("SELECT 1").fetchone()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_session_rolls_back_on_exception(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with sqlite_session(db) as conn:
                conn.execute(
                    "INSERT INTO runtime_saved_report VALUES (?, ?, ?, ?, ?)",
                    ("transient", "{}", "hash", "2026-08-18T00:00:00+00:00", None),
                )
                raise RuntimeError("synthetic")
        with sqlite_session(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM runtime_saved_report").fetchone()[0]
        self.assertEqual(count, 0)

    def test_malformed_durable_row_rolls_back_all_sections(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        state = {
            "agent_tokens": [
                {
                    "token_hash": "hash-1",
                    "agent_id": "agent-1",
                    "label": None,
                    "active": 1,
                    "created_at": "2026-08-18T00:00:00+00:00",
                    "quota_override": 17,
                    "expires_at": None,
                }
            ],
            "saved_reports": [
                {
                    "report_id": "report-1",
                    "payload_json": '{"synthetic": true}',
                    "access_token_hash": "hash-2",
                    "created_at": "not-a-timestamp",
                    "agent_id": "agent-1",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "timestamp"):
            restore_durable_state(db, state)
        with sqlite_session(db) as conn:
            token_count = conn.execute("SELECT COUNT(*) FROM ref_agent_token").fetchone()[0]
            report_count = conn.execute("SELECT COUNT(*) FROM runtime_saved_report").fetchone()[0]
        self.assertEqual((token_count, report_count), (0, 0))

    def test_insert_rejects_invalid_quota_and_non_object_report(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        with self.assertRaises(ValueError):
            insert_agent_token(
                db,
                token_hash="hash",
                agent_id="agent",
                label=None,
                created_at="2026-08-18T00:00:00+00:00",
                quota_override=-1,
            )
        with self.assertRaisesRegex(ValueError, "JSON object"):
            insert_saved_report(
                db,
                report_id="report",
                agent_id="agent",
                created_at="2026-08-18T00:00:00+00:00",
                payload_json="[]",
            )

    def test_insert_rejects_nonstandard_nonfinite_json_constants(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        for payload in (
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
            '{"nested": [1, {"value": NaN}]}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "JSON"):
                    insert_saved_report(
                        db,
                        report_id="report",
                        agent_id="agent",
                        created_at="2026-08-18T00:00:00+00:00",
                        payload_json=payload,
                    )
        insert_saved_report(
            db,
            report_id="string-is-valid",
            agent_id="agent",
            created_at="2026-08-18T00:00:00+00:00",
            payload_json='{"value": "NaN"}',
        )
        insert_saved_report(
            db,
            report_id="extreme-standard-number",
            agent_id="agent",
            created_at="2026-08-18T00:00:00+00:00",
            payload_json='{"value": 1e9999}',
        )

    def test_restore_rejects_nonstandard_json_and_rolls_back_every_section(self) -> None:
        db = self.root / "runtime.db"
        create_runtime_database(db, publication_id="one")
        state = {
            "agent_tokens": [
                {
                    "token_hash": "hash-1",
                    "agent_id": "agent-1",
                    "label": None,
                    "active": 1,
                    "created_at": "2026-08-18T00:00:00+00:00",
                    "quota_override": None,
                    "expires_at": None,
                }
            ],
            "saved_reports": [
                {
                    "report_id": "report-1",
                    "payload_json": '{"value": NaN}',
                    "access_token_hash": "hash-2",
                    "created_at": "2026-08-18T00:00:00+00:00",
                    "agent_id": "agent-1",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "JSON"):
            restore_durable_state(db, state)

        with sqlite_session(db) as connection:
            token_count = connection.execute("SELECT COUNT(*) FROM ref_agent_token").fetchone()[0]
            report_count = connection.execute("SELECT COUNT(*) FROM runtime_saved_report").fetchone()[0]
        self.assertEqual((token_count, report_count), (0, 0))
