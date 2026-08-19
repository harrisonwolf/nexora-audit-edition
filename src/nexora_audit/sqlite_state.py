"""Close-owning SQLite sessions and strict durable-row carry-over."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RUNTIME_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE runtime_manifest (
  publication_id TEXT NOT NULL
);

CREATE TABLE ref_agent_token (
  token_hash TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  label TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  quota_override INTEGER,
  expires_at TEXT
);

CREATE TABLE runtime_saved_report (
  report_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  access_token_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  agent_id TEXT
);
"""

AGENT_TOKEN_COLUMNS: tuple[str, ...] = (
    "token_hash",
    "agent_id",
    "label",
    "active",
    "created_at",
    "quota_override",
    "expires_at",
)
SAVED_REPORT_COLUMNS: tuple[str, ...] = (
    "report_id",
    "payload_json",
    "access_token_hash",
    "created_at",
    "agent_id",
)
DURABLE_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("agent_tokens", "ref_agent_token", AGENT_TOKEN_COLUMNS),
    ("saved_reports", "runtime_saved_report", SAVED_REPORT_COLUMNS),
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def sqlite_session(path: Path) -> Iterable[sqlite3.Connection]:
    """Commit or roll back, then close the handle unconditionally."""
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_runtime_database(path: Path, *, publication_id: str) -> None:
    if path.exists():
        raise FileExistsError(f"runtime database already exists: {path}")
    if not publication_id:
        raise ValueError("publication_id must be non-empty")
    with sqlite_session(path) as connection:
        connection.executescript(RUNTIME_SCHEMA)
        connection.execute(
            "INSERT INTO runtime_manifest (publication_id) VALUES (?)",
            (publication_id,),
        )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def read_durable_state(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read legacy-compatible durable rows without creating or migrating tables."""
    if not path.is_file():
        raise FileNotFoundError(path)
    durable: dict[str, list[dict[str, Any]]] = {}
    with sqlite_session(path) as connection:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for public_name, table, columns in DURABLE_TABLES:
            if table not in present:
                durable[public_name] = []
                continue
            available = _table_columns(connection, table)
            projection = ", ".join(
                column if column in available else f"NULL AS {column}"
                for column in columns
            )
            rows = connection.execute(
                f"SELECT {projection} FROM {table} ORDER BY {columns[0]}"
            ).fetchall()
            durable[public_name] = [
                {column: row[column] for column in columns}
                for row in rows
            ]
    return durable


def _require_text(value: object, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _require_timestamp(value: object, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    _require_text(value, field)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} timestamp must include a UTC offset")


def _validate_durable_row(identity: str, row: Mapping[str, object]) -> None:
    if identity == "agent_tokens":
        _require_text(row["token_hash"], "token_hash")
        _require_text(row["agent_id"], "agent_id")
        if row["label"] is not None and not isinstance(row["label"], str):
            raise ValueError("label must be a string or null")
        if type(row["active"]) is not int or row["active"] not in (0, 1):
            raise ValueError("active must be exactly 0 or 1")
        _require_timestamp(row["created_at"], "created_at")
        quota = row["quota_override"]
        if quota is not None and (type(quota) is not int or quota < 0):
            raise ValueError("quota_override must be a non-negative integer or null")
        _require_timestamp(row["expires_at"], "expires_at", optional=True)
        return

    if identity == "saved_reports":
        _require_text(row["report_id"], "report_id")
        _require_text(row["payload_json"], "payload_json")
        assert isinstance(row["payload_json"], str)
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise ValueError("payload_json must contain valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("payload_json must contain a JSON object")
        _require_text(row["access_token_hash"], "access_token_hash")
        _require_timestamp(row["created_at"], "created_at")
        _require_text(row["agent_id"], "agent_id", optional=True)
        return

    raise ValueError(f"unknown durable-state section: {identity}")


def _validated_rows(
    rows: object,
    *,
    columns: tuple[str, ...],
    identity: str,
) -> list[tuple[Any, ...]]:
    if not isinstance(rows, list):
        raise ValueError(f"{identity} durable state must be a list")
    validated: list[tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(columns):
            raise ValueError(f"{identity} durable row has unexpected fields")
        values = tuple(row[column] for column in columns)
        if not isinstance(values[0], str) or not values[0]:
            raise ValueError(f"{identity} durable row has an empty identity")
        _validate_durable_row(identity, row)
        validated.append(values)
    return validated


def restore_durable_state(path: Path, state: Mapping[str, object]) -> None:
    if set(state) != {"agent_tokens", "saved_reports"}:
        raise ValueError("durable state has unexpected sections")
    with sqlite_session(path) as connection:
        for public_name, table, columns in DURABLE_TABLES:
            rows = _validated_rows(state[public_name], columns=columns, identity=public_name)
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                rows,
            )


def insert_agent_token(
    path: Path,
    *,
    token_hash: str,
    agent_id: str,
    label: str | None,
    created_at: str,
    quota_override: int | None = None,
    expires_at: str | None = None,
) -> None:
    row = {
        "token_hash": token_hash,
        "agent_id": agent_id,
        "label": label,
        "active": 1,
        "created_at": created_at,
        "quota_override": quota_override,
        "expires_at": expires_at,
    }
    _validate_durable_row("agent_tokens", row)
    with sqlite_session(path) as connection:
        connection.execute(
            """
            INSERT INTO ref_agent_token
              (token_hash, agent_id, label, active, created_at, quota_override, expires_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (token_hash, agent_id, label, created_at, quota_override, expires_at),
        )


def insert_saved_report(
    path: Path,
    *,
    report_id: str,
    agent_id: str | None,
    created_at: str,
    payload_json: str,
) -> None:
    row = {
        "report_id": report_id,
        "payload_json": payload_json,
        "access_token_hash": f"synthetic-{report_id}",
        "created_at": created_at,
        "agent_id": agent_id,
    }
    _validate_durable_row("saved_reports", row)
    with sqlite_session(path) as connection:
        connection.execute(
            """
            INSERT INTO runtime_saved_report
              (report_id, payload_json, access_token_hash, created_at, agent_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, payload_json, f"synthetic-{report_id}", created_at, agent_id),
        )


def rebuild_runtime_database(old_path: Path, new_path: Path, *, publication_id: str) -> None:
    """Build a new volatile generation, then restore validated durable rows."""
    durable = read_durable_state(old_path)
    create_runtime_database(new_path, publication_id=publication_id)
    restore_durable_state(new_path, durable)
