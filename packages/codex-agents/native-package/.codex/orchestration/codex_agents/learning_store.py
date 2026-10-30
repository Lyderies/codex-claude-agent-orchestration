"""Owner-only state, SQLite ledger, and bounded local collector protocol."""

from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:  # AF_UNIX collector is currently supported on POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover - Windows has no supported collector transport yet.
    fcntl = None  # type: ignore[assignment]

from .contracts import (
    RESULT_VALIDATION_REASON_CODES,
    ContractError,
    result_axes,
    routing_task_slice,
    validate_assignment_shape,
    validate_result,
    validate_task,
)
from .learning_contracts import (
    MAX_RECORD_BYTES,
    RECORD_TYPES,
    canonical_json,
    content_digest,
    external_id,
    keyed_digest,
    parse_bounded_json,
    validate_learning_record,
    validate_event,
    validate_execution_configuration,
)
from .learning_adapters import HOOK_EVENTS, empty_usage, load_client_capabilities, reconcile_run


def _collector_runtime_digest() -> str:
    orchestration_root = Path(__file__).resolve().parent.parent
    paths = [
        orchestration_root / "cli.py",
        orchestration_root / "config" / "learning-clients.json",
        *sorted((orchestration_root / "codex_agents").glob("*.py")),
    ]
    hasher = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(orchestration_root).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(4, "big"))
        hasher.update(relative)
        content = path.read_bytes()
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


def _package_version() -> str:
    bundle_root = Path(__file__).resolve().parent.parent.parent.parent
    manifest_path = bundle_root / ".codex" / "orchestration" / "install-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("installed package manifest is invalid") from exc
        value = manifest.get("version") if isinstance(manifest, dict) else None
    else:
        value = (bundle_root / "VERSION").read_text(encoding="ascii").strip()
    if not isinstance(value, str):
        raise RuntimeError("package version is unavailable")
    if len(value.split(".")) != 3 or not all(
        part.isdigit() for part in value.split(".")
    ):
        raise RuntimeError("package VERSION must be a three-part numeric version")
    return value


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


LEDGER_SCHEMA_VERSION = 6
COLLECTOR_PROTOCOL_VERSION = 1
COLLECTOR_RUNTIME_DIGEST = _collector_runtime_digest()
PACKAGE_VERSION = _package_version()
COLLECTOR_AUTHORIZATION_SCHEMA_VERSION = 1
DEFAULT_RETENTION_MS = 90 * 24 * 60 * 60 * 1000
REPAIR_RECEIPT_TTL_MS = 24 * 60 * 60 * 1000
MAX_PENDING_REPAIR_RECEIPTS = 4096
MAX_PENDING_HOST_RUNS = 4096
HOST_RUN_TTL_MS = 24 * 60 * 60 * 1000
OPERATIONAL_CHECK_TTL_MS = 60 * 60 * 1000
UPGRADE_RECONCILIATION_TTL_MS = 10 * 60 * 1000
MAX_PENDING_OPERATIONAL_CHECKS = 256
INGESTION_FRESH_MS = 30 * 60 * 1000
MAX_REQUEST_BYTES = MAX_RECORD_BYTES + 16_384
PRODUCER_AUTHORITY = {
    "hook": {"event"},
    "app_server": {"event"},
    "host": {"event", "learning_export"},
    "evaluator": {
        "outcome", "evaluation_case", "evaluation_assignment", "evaluation_trial", "candidate",
        "comparison", "canary_report",
    },
    "verifier": {"outcome", "qa_evidence", "evidence_review"},
    "owner": {
        "feedback", "owner_grant", "grader_calibration", "billing_attestation",
    },
    "lifecycle": {"promotion"},
}
# Only ledger methods that run the observation themselves may write these.
# Billing evidence younger than this is reused instead of probing again.
PROBE_REUSE_SECONDS = 300
PROVIDER_OBSERVATION_TYPES = {
    "provider_identity", "provider_headroom", "provider_billing",
    "observation_unavailable", "provider_usage",
}
# A run keeps only rate-limit events more restrictive than those it already
# recorded, so this cap is reached only by a stream that keeps escalating.
MAX_RUN_RATE_LIMIT_RECORDS = 64
CLAUDE_STATUS_RANK = {None: 0, "allowed": 0, "allowed_warning": 1, "rejected": 2}
BILLING_RANK = {False: 0, None: 1, True: 2}
INSTALLED_ROUTING_POLICY = (
    Path(__file__).resolve().parent.parent / "config" / "routing-policy.json"
)
SOURCE_BY_PRODUCER = {
    "hook": "hook",
    "app_server": "app_server",
    "host": "host",
    "evaluator": "evaluator",
    "verifier": "verifier",
    "owner": "owner",
    "lifecycle": "lifecycle",
    "provider": "provider",
}
HOOK_EVENT_CODES = tuple(value[0] for value in HOOK_EVENTS.values())
HOOK_REJECTION_CODES = {
    "hook_normalization_rejected",
    "hook_binding_rejected",
    "hook_ingest_rejected",
}


def empty_ingestion_snapshot(*, registered: bool) -> dict[str, Any]:
    """Return the closed aggregate shape without exposing project or record identity."""

    return {
        "registered_project_state": "active" if registered else "unregistered",
        "ingestion_state": "active" if registered else "inactive",
        "accepted_hook_event_count": 0,
        "last_accepted_hook_event_at_ms": None,
        "rejected_hook_event_count": 0,
        "last_rejected_hook_event_at_ms": None,
        "freshness": {
            "state": (
                "awaiting_hook_review_or_first_event" if registered else "unregistered"
            ),
            "age_ms": None,
        },
        "hook_event_counts": {code: 0 for code in HOOK_EVENT_CODES},
        "coverage": {
            "root_lifecycle": True,
            "child_lifecycle": True,
            "tool_lifecycle": True,
            "root_usage": False,
            "child_usage": False,
        },
        "managed_lifecycle": {
            "orchestrations_active": 0,
            "orchestrations_paused": 0,
            "orchestrations_closed": 0,
            "host_runs_opened": 0,
            "host_runs_terminal": 0,
            "host_runs_pending": 0,
            "host_runs_abandoned": 0,
            "native_agent_bindings": 0,
            "operational_qa_passed": 0,
            "operational_qa_failed": 0,
            "host_usage_complete": 0,
            "host_usage_unknown": 0,
        },
    }


class StateError(RuntimeError):
    """Raised when local state would violate an accepted boundary."""


class LedgerError(RuntimeError):
    """Raised for controlled ledger failures without echoing record content."""


class ProjectNotRegisteredError(LedgerError):
    """Raised when an exact project root has no active registration."""


class CollectorError(RuntimeError):
    """Raised for bounded collector protocol failures."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class CollectorVersionError(CollectorError):
    """Raised when a healthy collector runs a different package build."""


class CollectorUpgradeRequired(CollectorVersionError):
    """Raised when the active collector is older than this installed package."""


class CollectorNewerVersionError(CollectorVersionError):
    """Raised without replacement when a newer collector owns shared state."""


class CollectorBuildConflictError(CollectorVersionError):
    """Raised without replacement for divergent builds with the same version."""


class CollectorAuthorizationError(CollectorVersionError):
    """Raised when this package is not the owner-authorized collector runtime."""


class CollectorRestartError(CollectorError):
    """Raised after a ledger operation committed but collector recovery failed."""


_BACKUP_PROJECT_COLUMNS = (
    "project_id", "canonical_root", "root_digest", "status", "created_at_ms",
    "deleted_at_ms",
)
_BACKUP_TABLE_COLUMNS = {
    "sessions": ("session_digest", "producer", "created_at_ms"),
    "run_provenance": (
        "run_id", "producer", "client_version", "session_digest", "created_at_ms",
    ),
    "records": (
        "record_id", "record_type", "source", "source_sequence", "observed_at_ms",
        "ingested_at_ms", "expires_at_ms", "content", "content_digest", "late",
    ),
    "record_links": ("from_record_id", "to_record_id", "relation"),
    "stored_files": (
        "file_id", "kind", "relative_path", "content_digest", "source_record_id",
        "expires_at_ms",
    ),
    "collector_health": ("code", "count", "updated_at_ms"),
    "consumed_grants": (
        "grant_id", "promotion_record_id", "candidate_digest", "action",
        "consumed_at_ms",
    ),
    "owner_attestations": ("subject_record_id", "signature", "attested_at_ms"),
    "repair_receipts": (
        "receipt_id", "assignment_digest", "reason_code", "issued_at_ms",
        "expires_at_ms", "consumed_at_ms", "run_id",
    ),
    "host_run_bindings": (
        "handle_digest", "run_id", "assignment_digest", "task_digest",
        "task_id_digest", "route_digest", "configuration_digest", "client_version",
        "created_at_ms", "expires_at_ms", "consumed_at_ms", "orchestration_id",
        "native_assignment_id", "agent_type", "usage_complete", "binding_state",
        "abandoned_reason", "observation_started_at_ms",
        "observation_deadline_at_ms", "last_progress_at_ms", "deadline_observed",
    ),
    "hook_ingestion": ("event_code", "accepted_count", "last_accepted_at_ms"),
    "orchestrations": (
        "orchestration_id", "handle_digest", "client_version", "status",
        "started_at_ms", "ended_at_ms", "goal_tokens_used",
        "goal_time_used_seconds", "expires_at_ms",
    ),
    "orchestration_checkpoints": (
        "orchestration_id", "sequence", "status", "goal_tokens_used",
        "goal_time_used_seconds", "observed_at_ms",
    ),
    "operational_qa": (
        "qa_id", "qa_digest", "host_outcome_digest",
        "verifier_host_outcome_digest", "accepted", "observed_at_ms", "content",
    ),
    "operational_check_receipts": (
        "receipt_id", "report_digest", "host_outcome_digest",
        "verifier_host_outcome_digest", "orchestration_id", "issued_at_ms",
        "expires_at_ms", "consumed_at_ms", "report",
    ),
    "tombstones": ("deletion_id", "deleted_at_ms"),
}
_BACKUP_V5_HOST_COLUMNS = tuple(
    column for column in _BACKUP_TABLE_COLUMNS["host_run_bindings"]
    if column not in {
        "binding_state", "abandoned_reason", "observation_started_at_ms",
        "observation_deadline_at_ms", "last_progress_at_ms", "deadline_observed",
    }
)
_BACKUP_ORDER_COLUMNS = {
    "sessions": ("session_digest", "producer"),
    "run_provenance": ("run_id", "producer"),
    "records": ("source_sequence",),
    "record_links": ("from_record_id", "to_record_id", "relation"),
    "stored_files": ("file_id",),
    "collector_health": ("code",),
    "consumed_grants": ("grant_id",),
    "owner_attestations": ("subject_record_id",),
    "repair_receipts": ("receipt_id",),
    "host_run_bindings": ("created_at_ms", "run_id"),
    "hook_ingestion": ("event_code",),
    "orchestrations": ("started_at_ms", "orchestration_id"),
    "orchestration_checkpoints": ("orchestration_id", "sequence"),
    "operational_qa": ("observed_at_ms", "qa_id"),
    "operational_check_receipts": ("issued_at_ms", "receipt_id"),
    "tombstones": ("deletion_id",),
}
_BACKUP_BYTES_KEY = "$codex_agents_bytes_base64"


def _backup_encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BACKUP_BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _backup_decode_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {_BACKUP_BYTES_KEY}:
        encoded = value[_BACKUP_BYTES_KEY]
        if not isinstance(encoded, str):
            raise LedgerError("project backup byte encoding is invalid")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LedgerError("project backup byte encoding is invalid") from exc
    if isinstance(value, dict):
        raise LedgerError("project backup contains an unsupported object value")
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise LedgerError("project backup contains an unsupported value")
    return value


def _backup_expected_columns(schema_version: int, table: str) -> tuple[str, ...]:
    if schema_version == 5:
        if table == "host_run_bindings":
            return _BACKUP_V5_HOST_COLUMNS
        if table == "orchestration_checkpoints":
            return ()
    return _BACKUP_TABLE_COLUMNS[table]


def _snapshot_project_partition(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    ledger_schema_version: int,
) -> dict[str, Any]:
    project_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(projects)").fetchall()
    }
    if project_columns != set(_BACKUP_PROJECT_COLUMNS):
        raise LedgerError("project backup cannot represent projects exactly")
    project = connection.execute(
        f"SELECT {','.join(_BACKUP_PROJECT_COLUMNS)} FROM projects "
        "WHERE project_id=? AND status='active'",
        (project_id,),
    ).fetchone()
    if project is None:
        raise LedgerError("project binding is not active")
    tables: dict[str, list[dict[str, Any]]] = {}
    columns_manifest: dict[str, list[str]] = {}
    for table, wanted in _BACKUP_TABLE_COLUMNS.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        available = (
            {str(row[1]) for row in connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()}
            if exists is not None else set()
        )
        expected = list(_backup_expected_columns(ledger_schema_version, table))
        expected_available = {"project_id", *expected} if expected else set()
        if available != expected_available:
            raise LedgerError(f"project backup cannot represent {table} exactly")
        columns = expected
        columns_manifest[table] = columns
        if not columns:
            tables[table] = []
            continue
        order = [
            column for column in _BACKUP_ORDER_COLUMNS[table] if column in columns
        ]
        query = (
            f"SELECT {','.join(columns)} FROM {table} WHERE project_id=?"
            + (f" ORDER BY {','.join(order)}" if order else "")
        )
        tables[table] = [
            {
                column: _backup_encode_value(row[column])
                for column in columns
            }
            for row in connection.execute(query, (project_id,)).fetchall()
        ]
    return {
        "schema_version": 2,
        "ledger_schema_version": ledger_schema_version,
        "project": {
            column: _backup_encode_value(project[column])
            for column in _BACKUP_PROJECT_COLUMNS
        },
        "columns": columns_manifest,
        "tables": tables,
        "counts": {name: len(rows) for name, rows in tables.items()},
    }


_BACKUP_RESTORE_SCHEMA = """
CREATE TABLE projects(project_id TEXT PRIMARY KEY,canonical_root TEXT,root_digest TEXT,
 status TEXT NOT NULL CHECK(status IN ('active','deleted')),created_at_ms INTEGER NOT NULL,
 deleted_at_ms INTEGER);
CREATE TABLE sessions(project_id TEXT NOT NULL,session_digest TEXT NOT NULL,producer TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL,PRIMARY KEY(project_id,session_digest,producer),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE run_provenance(project_id TEXT NOT NULL,run_id TEXT NOT NULL,producer TEXT NOT NULL,
 client_version TEXT NOT NULL,session_digest TEXT NOT NULL,created_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,run_id,producer),FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE records(project_id TEXT NOT NULL,record_id TEXT NOT NULL,record_type TEXT NOT NULL,
 source TEXT NOT NULL,source_sequence INTEGER NOT NULL,observed_at_ms INTEGER NOT NULL,
 ingested_at_ms INTEGER NOT NULL,expires_at_ms INTEGER,content BLOB NOT NULL,
 content_digest TEXT NOT NULL,late INTEGER NOT NULL CHECK(late IN (0,1)),
 PRIMARY KEY(project_id,record_id),UNIQUE(project_id,source_sequence),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE record_links(project_id TEXT NOT NULL,from_record_id TEXT NOT NULL,to_record_id TEXT NOT NULL,
 relation TEXT NOT NULL,PRIMARY KEY(project_id,from_record_id,to_record_id,relation),
 FOREIGN KEY(project_id,from_record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE,
 FOREIGN KEY(project_id,to_record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE);
CREATE TABLE stored_files(project_id TEXT NOT NULL,file_id TEXT NOT NULL,kind TEXT NOT NULL
 CHECK(kind IN ('artifact','export','backup')),relative_path TEXT NOT NULL,content_digest TEXT NOT NULL,
 source_record_id TEXT,expires_at_ms INTEGER,PRIMARY KEY(project_id,file_id),UNIQUE(project_id,relative_path),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
 FOREIGN KEY(project_id,source_record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE);
CREATE TABLE collector_health(project_id TEXT NOT NULL,code TEXT NOT NULL,count INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,PRIMARY KEY(project_id,code),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE consumed_grants(project_id TEXT NOT NULL,grant_id TEXT NOT NULL,promotion_record_id TEXT NOT NULL,
 candidate_digest TEXT NOT NULL,action TEXT NOT NULL,consumed_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,grant_id),FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
 FOREIGN KEY(project_id,grant_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE,
 FOREIGN KEY(project_id,promotion_record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE);
CREATE TABLE owner_attestations(project_id TEXT NOT NULL,subject_record_id TEXT NOT NULL,
 signature BLOB NOT NULL,attested_at_ms INTEGER NOT NULL,PRIMARY KEY(project_id,subject_record_id),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
 FOREIGN KEY(project_id,subject_record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE);
CREATE TABLE repair_receipts(project_id TEXT NOT NULL,receipt_id TEXT NOT NULL,
 assignment_digest TEXT NOT NULL,reason_code TEXT NOT NULL,issued_at_ms INTEGER NOT NULL,
 expires_at_ms INTEGER NOT NULL,consumed_at_ms INTEGER,run_id TEXT,PRIMARY KEY(project_id,receipt_id),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE host_run_bindings(project_id TEXT NOT NULL,handle_digest TEXT NOT NULL,run_id TEXT NOT NULL,
 assignment_digest TEXT NOT NULL,task_digest TEXT NOT NULL,task_id_digest TEXT,route_digest TEXT NOT NULL,
 configuration_digest TEXT NOT NULL,client_version TEXT NOT NULL,created_at_ms INTEGER NOT NULL,
 expires_at_ms INTEGER NOT NULL,consumed_at_ms INTEGER,orchestration_id TEXT,native_assignment_id TEXT,
 agent_type TEXT,usage_complete INTEGER,binding_state TEXT,abandoned_reason TEXT,
 observation_started_at_ms INTEGER,observation_deadline_at_ms INTEGER,last_progress_at_ms INTEGER,
 deadline_observed INTEGER,PRIMARY KEY(project_id,handle_digest),UNIQUE(project_id,run_id),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE hook_ingestion(project_id TEXT NOT NULL,event_code TEXT NOT NULL,
 accepted_count INTEGER NOT NULL CHECK(accepted_count>=0),last_accepted_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,event_code),FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE orchestrations(project_id TEXT NOT NULL,orchestration_id TEXT NOT NULL,handle_digest TEXT NOT NULL,
 client_version TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN
 ('active','paused','completed','failed','interrupted','cancelled')),started_at_ms INTEGER NOT NULL,
 ended_at_ms INTEGER,goal_tokens_used INTEGER,goal_time_used_seconds INTEGER,expires_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,orchestration_id),UNIQUE(project_id,handle_digest),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE orchestration_checkpoints(project_id TEXT NOT NULL,orchestration_id TEXT NOT NULL,
 sequence INTEGER NOT NULL,status TEXT NOT NULL,goal_tokens_used INTEGER,goal_time_used_seconds INTEGER,
 observed_at_ms INTEGER NOT NULL,PRIMARY KEY(project_id,orchestration_id,sequence),
 FOREIGN KEY(project_id,orchestration_id) REFERENCES orchestrations(project_id,orchestration_id) ON DELETE CASCADE);
CREATE TABLE operational_qa(project_id TEXT NOT NULL,qa_id TEXT NOT NULL,qa_digest TEXT NOT NULL,
 host_outcome_digest TEXT NOT NULL,verifier_host_outcome_digest TEXT NOT NULL,accepted INTEGER NOT NULL
 CHECK(accepted IN (0,1)),observed_at_ms INTEGER NOT NULL,content BLOB NOT NULL,
 PRIMARY KEY(project_id,qa_id),UNIQUE(project_id,host_outcome_digest),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE operational_check_receipts(project_id TEXT NOT NULL,receipt_id TEXT NOT NULL,
 report_digest TEXT NOT NULL,host_outcome_digest TEXT NOT NULL,verifier_host_outcome_digest TEXT NOT NULL,
 orchestration_id TEXT NOT NULL,issued_at_ms INTEGER NOT NULL,expires_at_ms INTEGER NOT NULL,
 consumed_at_ms INTEGER,report BLOB NOT NULL,PRIMARY KEY(project_id,receipt_id),
 FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);
CREATE TABLE tombstones(project_id TEXT NOT NULL,deletion_id TEXT NOT NULL,deleted_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,deletion_id));
"""


def _verify_project_backup_body(
    body: Any,
    *,
    expected_project_id: str | None = None,
    expected_ledger_schema_version: int | None = None,
) -> dict[str, int]:
    if not isinstance(body, dict) or set(body) != {
        "schema_version", "ledger_schema_version", "project", "columns", "tables",
        "counts",
    }:
        raise LedgerError("project backup body is invalid")
    schema_version = body["ledger_schema_version"]
    if body["schema_version"] != 2 or schema_version not in {5, 6}:
        raise LedgerError("project backup body is invalid")
    if (
        expected_ledger_schema_version is not None
        and schema_version != expected_ledger_schema_version
    ):
        raise LedgerError("project backup ledger version is invalid")
    project = body["project"]
    if not isinstance(project, dict) or set(project) != set(_BACKUP_PROJECT_COLUMNS):
        raise LedgerError("project backup project row is invalid")
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or (
        expected_project_id is not None and project_id != expected_project_id
    ):
        raise LedgerError("project backup project partition is invalid")
    tables, counts, columns = body["tables"], body["counts"], body["columns"]
    expected_tables = set(_BACKUP_TABLE_COLUMNS)
    if (
        not isinstance(tables, dict) or set(tables) != expected_tables
        or not isinstance(counts, dict) or set(counts) != expected_tables
        or not isinstance(columns, dict) or set(columns) != expected_tables
    ):
        raise LedgerError("project backup table manifest is invalid")
    restored = sqlite3.connect(":memory:")
    restored.row_factory = sqlite3.Row
    restored.execute("PRAGMA foreign_keys = ON")
    try:
        restored.executescript(_BACKUP_RESTORE_SCHEMA)
        project_values = [_backup_decode_value(project[name]) for name in _BACKUP_PROJECT_COLUMNS]
        restored.execute(
            f"INSERT INTO projects({','.join(_BACKUP_PROJECT_COLUMNS)}) VALUES(?,?,?,?,?,?)",
            project_values,
        )
        decoded_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in _BACKUP_TABLE_COLUMNS:
            declared = columns[table]
            expected = list(_backup_expected_columns(schema_version, table))
            rows = tables[table]
            if declared != expected or not isinstance(rows, list) or counts[table] != len(rows):
                raise LedgerError("project backup count or column manifest is invalid")
            decoded_rows = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(declared):
                    raise LedgerError("project backup row shape is invalid")
                decoded = {name: _backup_decode_value(row[name]) for name in declared}
                decoded_rows.append(decoded)
                if table == "records":
                    try:
                        content = validate_learning_record(json.loads(bytes(decoded["content"])))
                    except (ContractError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise LedgerError("project backup contains a corrupt learning record") from exc
                    if content_digest(content) != decoded["content_digest"]:
                        raise LedgerError("project backup contains a corrupt learning record")
            decoded_by_table[table] = decoded_rows
            if declared:
                placeholders = ",".join("?" for _ in range(len(declared) + 1))
                try:
                    restored.executemany(
                        f"INSERT INTO {table}(project_id,{','.join(declared)}) VALUES({placeholders})",
                        [
                            [project_id, *(row[name] for name in declared)]
                            for row in decoded_rows
                        ],
                    )
                except sqlite3.IntegrityError as exc:
                    raise LedgerError("isolated project backup restoration rejected a row") from exc
        foreign_keys = restored.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise LedgerError("isolated project backup foreign-key check failed")
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise LedgerError("isolated project backup integrity check failed")
        for table, declared in columns.items():
            if not declared:
                continue
            actual = [
                {name: _backup_encode_value(row[name]) for name in declared}
                for row in restored.execute(
                    f"SELECT {','.join(declared)} FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            ]
            expected_rows = tables[table]
            if sorted(canonical_json(row) for row in actual) != sorted(
                canonical_json(row) for row in expected_rows
            ):
                raise LedgerError("isolated project backup restoration changed rows")
    finally:
        restored.close()
    return {name: int(counts[name]) for name in _BACKUP_TABLE_COLUMNS}


def _verify_backup_envelope(
    raw: bytes,
    *,
    expected_project_id: str | None = None,
    expected_ledger_schema_version: int | None = None,
) -> tuple[dict[str, Any], dict[str, int], str]:
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("project backup is invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"backup_sha256", "body"}:
        raise LedgerError("project backup envelope is invalid")
    manifest_digest = envelope["backup_sha256"]
    body = envelope["body"]
    if (
        not isinstance(manifest_digest, str)
        or hashlib.sha256(canonical_json(body)).hexdigest() != manifest_digest
    ):
        raise LedgerError("project backup manifest digest is invalid")
    counts = _verify_project_backup_body(
        body,
        expected_project_id=expected_project_id,
        expected_ledger_schema_version=expected_ledger_schema_version,
    )
    return body, counts, manifest_digest


def default_state_root() -> Path:
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-agents"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise StateError("LOCALAPPDATA is required on Windows")
        return Path(base) / "codex-agents"
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) / "codex-agents" if base else Path.home() / ".local/state/codex-agents"


def sys_platform() -> str:
    import sys

    return sys.platform


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists():
            continue
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise StateError("state path cannot be inspected") from exc
        if stat.S_ISLNK(mode):
            raise StateError("state path contains a symlink component")


def _check_owner_mode(path: Path, *, directory: bool) -> None:
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StateError("state path is not owned by the current user")
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(info.st_mode) != expected:
        raise StateError("state path has unsafe permissions")


class LearningState:
    """Validated paths and the local keyed-digest secret."""

    def __init__(self, root: Path | None = None, *, create: bool = True) -> None:
        requested = (root or default_state_root()).expanduser()
        if not requested.is_absolute():
            raise StateError("state root must be absolute")
        _assert_no_symlink_components(requested)
        if _inside_git_tree(requested):
            raise StateError("state root must be outside a Git working tree")
        existed = requested.exists()
        if create:
            requested.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not existed:
                os.chmod(requested, 0o700)
        if not requested.is_dir():
            raise StateError("state root must be a directory")
        resolved = requested.resolve(strict=True)
        _assert_no_symlink_components(resolved)
        _check_owner_mode(resolved, directory=True)
        self.root = resolved
        self.artifacts = self._directory("artifacts", create=create)
        self.exports = self._directory("exports", create=create)
        self.backups = self._directory("backups", create=create)
        self.database = self.root / "learning.sqlite3"
        self.socket = self.root / "collector.sock"
        self.lock = self.root / "collector.lock"
        self.runtime_lock = self.root / "collector-runtime.lock"
        self.collector_authorization = self.root / "collector-authorization.json"
        self.client_health_lock = self.root / "collector-client-health.lock"
        self.client_health = self.root / "collector-client-health.json"
        self.key_path = self.root / "digest.key"
        self.allowed_signers_path = self.root / "owner-allowed-signers"
        if create:
            self._create_empty_file(self.database)
            self._create_key(self.key_path)
            self._create_empty_file(self.allowed_signers_path)
        if create:
            _check_owner_mode(self.database, directory=False)
            _check_owner_mode(self.key_path, directory=False)
            _check_owner_mode(self.allowed_signers_path, directory=False)

    @staticmethod
    def _create_key(path: Path) -> None:
        if path.exists():
            return
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(32))
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _create_empty_file(path: Path) -> None:
        if path.exists():
            return
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)

    def _directory(self, name: str, *, create: bool) -> Path:
        path = self.root / name
        if path.is_symlink():
            raise StateError("state path contains a symlink component")
        existed = path.exists()
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
            if not existed:
                os.chmod(path, 0o700)
        if path.exists():
            _assert_no_symlink_components(path)
            _check_owner_mode(path, directory=True)
        return path

    def digest_key(self) -> bytes:
        key = self._read_key(self.key_path, "digest")
        return key

    @staticmethod
    def _read_key(path: Path, label: str) -> bytes:
        if path.is_symlink():
            raise StateError(f"{label} key path is unsafe")
        _check_owner_mode(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            key = handle.read(33)
        if len(key) != 32:
            raise StateError(f"{label} key has an invalid length")
        return key

    def install_owner_public_key(self, public_key: str) -> Path:
        """Install one public SSH key; private signing material never enters state."""

        fields = public_key.strip().split()
        allowed_types = {
            "ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ssh-rsa",
            "ecdsa-sha2-nistp256", "sk-ecdsa-sha2-nistp256@openssh.com",
        }
        if (
            len(fields) < 2
            or fields[0] not in allowed_types
            or not fields[1]
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for character in fields[1])
            or "PRIVATE" in public_key
        ):
            raise StateError("owner public key is invalid")
        content = f"codex-agents-owner {fields[0]} {fields[1]}\n".encode("ascii")
        existing = self.allowed_signers_path.read_bytes()
        if existing:
            if existing == content:
                return self.allowed_signers_path
            raise StateError(
                "owner public key is already configured; key rotation requires a reviewed action"
            )
        temporary = self.root / ".owner-allowed-signers.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.allowed_signers_path)
            os.chmod(self.allowed_signers_path, 0o600)
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        return self.allowed_signers_path

    def owner_allowed_signers(self) -> Path:
        if self.allowed_signers_path.is_symlink():
            raise StateError("owner allowed-signers path is unsafe")
        _check_owner_mode(self.allowed_signers_path, directory=False)
        content = self.allowed_signers_path.read_text(encoding="ascii")
        if not content.startswith("codex-agents-owner ") or len(content) > 16_384:
            raise StateError("owner public key is not configured")
        return self.allowed_signers_path

    def harden_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.database) + suffix)
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise StateError("database state contains an unsafe path")
                _check_owner_mode(path, directory=False)


def validate_collector_runtime(cli_path: Path) -> dict[str, str | int]:
    """Validate the complete installed package and return its collector identity."""

    from .learning_engine import _installed_managed_files, _read_managed_bytes

    try:
        if cli_path.is_symlink():
            raise ContractError("installed collector entry point is invalid")
        resolved_cli = cli_path.resolve(strict=True)
        bundle_root = resolved_cli.parents[2]
        expected_cli = bundle_root / ".codex" / "orchestration" / "cli.py"
        if resolved_cli != expected_cli:
            raise ContractError("installed collector entry point is invalid")
        paths, contents = _installed_managed_files(bundle_root)
        manifest_relative = ".codex/orchestration/install-manifest.json"
        manifest_path = bundle_root / manifest_relative
        if manifest_path.is_symlink():
            raise ContractError("installed package manifest is invalid")
        if manifest_path.exists():
            manifest = json.loads(
                _read_managed_bytes(bundle_root, manifest_relative)
            )
            version = manifest.get("version") if isinstance(manifest, dict) else None
        else:
            version_path = bundle_root / "VERSION"
            descriptor = os.open(
                version_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ContractError("package VERSION is invalid")
                version = os.read(descriptor, 33).decode("ascii").strip()
            finally:
                os.close(descriptor)
        if _version_tuple(version) is None:
            raise ContractError("package VERSION is invalid")
        prefix = ".codex/orchestration/"
        cli_relative = f"{prefix}cli.py"
        clients_relative = f"{prefix}config/learning-clients.json"
        module_paths = sorted(
            path
            for path in paths
            if path.startswith(f"{prefix}codex_agents/") and path.endswith(".py")
        )
        runtime_paths = [cli_relative, clients_relative, *module_paths]
        if (
            cli_relative not in contents
            or clients_relative not in contents
            or f"{prefix}codex_agents/learning_store.py" not in module_paths
        ):
            raise ContractError("installed collector runtime is incomplete")
        hasher = hashlib.sha256()
        for path in runtime_paths:
            relative = path.removeprefix(prefix).encode("utf-8")
            content = contents[path]
            hasher.update(len(relative).to_bytes(4, "big"))
            hasher.update(relative)
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)
        runtime_digest = hasher.hexdigest()
        if version != PACKAGE_VERSION or runtime_digest != COLLECTOR_RUNTIME_DIGEST:
            raise ContractError("installed collector identity is inconsistent")
        return {
            "schema_version": COLLECTOR_AUTHORIZATION_SCHEMA_VERSION,
            "runtime_version": version,
            "runtime_digest": runtime_digest,
        }
    except (
        ContractError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        IndexError,
    ) as exc:
        raise CollectorAuthorizationError(
            "installed package integrity validation failed",
            code="runtime_integrity_invalid",
        ) from exc


def _collector_runtime_authorization(
    state: LearningState, *, allow_quiesced: bool
) -> dict[str, str | int | bool]:
    """Read a valid active or upgrade-quiesced collector identity."""

    path = state.collector_authorization
    if not path.exists():
        raise CollectorAuthorizationError(
            "collector runtime is not authorized; run learning-register",
            code="runtime_authorization_missing",
        )
    if path.is_symlink():
        raise CollectorAuthorizationError(
            "collector runtime authorization is unsafe",
            code="runtime_authorization_invalid",
        )
    try:
        _check_owner_mode(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise StateError("collector runtime authorization is unsafe")
            raw = os.read(descriptor, 1025)
        finally:
            os.close(descriptor)
        if len(raw) > 1024:
            raise StateError("collector runtime authorization is invalid")
        value = json.loads(raw)
    except (OSError, StateError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) not in (
            {"schema_version", "runtime_version", "runtime_digest"},
            {
                "schema_version", "runtime_version", "runtime_digest",
                "quiesced_for_upgrade",
            },
        )
        or value.get("schema_version") != COLLECTOR_AUTHORIZATION_SCHEMA_VERSION
        or _version_tuple(value.get("runtime_version")) is None
        or not isinstance(value.get("runtime_digest"), str)
        or len(value["runtime_digest"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["runtime_digest"]
        )
    ):
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        )
    if value.get("quiesced_for_upgrade") is not True and "quiesced_for_upgrade" in value:
        raise CollectorAuthorizationError(
            "collector runtime authorization is invalid",
            code="runtime_authorization_invalid",
        )
    if value.get("quiesced_for_upgrade") is True and not allow_quiesced:
        raise CollectorAuthorizationError(
            "collector runtime is quiesced for an owner-authorized upgrade",
            code="runtime_authorization_quiesced",
        )
    return value


def authorized_collector_runtime(state: LearningState) -> dict[str, str | int]:
    """Read the exact active collector identity authorized in owner-only state."""

    return _collector_runtime_authorization(state, allow_quiesced=False)


def require_authorized_collector_runtime(
    state: LearningState,
) -> dict[str, str | int]:
    """Fail closed unless this process is the exact owner-authorized runtime."""

    authorized = authorized_collector_runtime(state)
    if (
        authorized["runtime_version"] != PACKAGE_VERSION
        or authorized["runtime_digest"] != COLLECTOR_RUNTIME_DIGEST
    ):
        raise CollectorAuthorizationError(
            "installed collector differs from the owner-authorized runtime; "
            "run learning-register from the intended package",
            code="runtime_unauthorized",
        )
    return authorized


def authorize_collector_runtime(
    state: LearningState, cli_path: Path
) -> dict[str, str | int]:
    """Authorize one validated runtime during explicit owner registration."""

    identity = validate_collector_runtime(cli_path)
    current: dict[str, str | int] | None = None
    if (
        state.collector_authorization.exists()
        or state.collector_authorization.is_symlink()
    ):
        current = _collector_runtime_authorization(state, allow_quiesced=True)
    if current is not None:
        current_version = _version_tuple(current["runtime_version"])
        requested_version = _version_tuple(identity["runtime_version"])
        if current_version is None or requested_version is None:  # validated above
            raise CollectorAuthorizationError("collector runtime version is invalid")
        if current_version > requested_version:
            raise CollectorNewerVersionError(
                "a newer collector runtime is already authorized",
                code="runtime_newer",
            )
        if current_version == requested_version:
            if current["runtime_digest"] != identity["runtime_digest"]:
                raise CollectorBuildConflictError(
                    "authorized collector build conflicts with this package version",
                    code="runtime_conflict",
                )
            if current.get("quiesced_for_upgrade") is not True:
                return current
    temporary = state.root / f".collector-authorization.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(identity) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state.collector_authorization)
        os.chmod(state.collector_authorization, 0o600)
        root_descriptor = os.open(state.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return identity


def _canonical_project_root(path: Path) -> Path:
    if not path.is_absolute():
        raise StateError("project root must be absolute")
    _assert_no_symlink_components(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise StateError("project root must be a directory")
    _assert_no_symlink_components(resolved)
    return resolved


@dataclass(frozen=True)
class SessionBinding:
    token: str
    project_id: str
    session_digest: str
    producer: str
    root_run_id: str
    coverage: str
    event_codes: frozenset[str]
    expires_at_ms: int


def _window_covered(window: dict[str, Any], earlier: dict[str, Any]) -> bool:
    """Whether an earlier window already restricts at least as much, as long."""

    def used(item: dict[str, Any]) -> float:
        return -1.0 if item["used_percent"] is None else float(item["used_percent"])

    if (earlier["scope"], earlier["window_minutes"]) != (
        window["scope"], window["window_minutes"]
    ) or used(earlier) < used(window):
        return False
    if window["resets_at"] is None or earlier["resets_at"] is None:
        return window["resets_at"] == earlier["resets_at"]
    return earlier["resets_at"] >= window["resets_at"]


def _more_restrictive(record: dict[str, Any], prior: list[dict[str, Any]]) -> bool:
    """Whether a run's new rate-limit record restricts more than its earlier ones.

    Billing is ranked usable over unknown over unusable, and only a higher rank
    is kept, because admission reads the latest billing record. A headroom
    record is kept for a higher status, or for any window that no earlier
    record covers. Only an earlier record at least as restrictive in status can
    cover a window, so a rejection is never hidden behind an allowed reading
    that happened to last longer.
    """

    if not prior:
        return True
    if record["record_type"] == "provider_billing":
        return BILLING_RANK[record["paid_usage_usable"]] > max(
            BILLING_RANK[item["paid_usage_usable"]] for item in prior
        )
    status = CLAUDE_STATUS_RANK[record["status"]]
    covering = [item for item in prior if CLAUDE_STATUS_RANK[item["status"]] >= status]
    if not covering:
        return True
    earlier = [window for item in covering for window in item["windows"]]
    return any(
        not any(_window_covered(window, other) for other in earlier)
        for window in record["windows"]
    )


def _merged_restriction(
    records: list[dict[str, Any]], *, observed_at_ms: int
) -> dict[str, Any]:
    """One headroom record as restrictive as all of a run's: the highest status,
    and per window the highest use and the latest effective reset.

    A window with no reset time counts for a day from its own observation, so
    that day's end is its effective reset. The merged window always carries an
    explicit reset at least as late as every input's, so it never ends sooner.
    """

    from .providers import EVIDENCE_HORIZON_MS, headroom_record

    status = max((item["status"] for item in records), key=CLAUDE_STATUS_RANK.__getitem__)
    windows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in records:
        for window in item["windows"]:
            key = (window["scope"], window["window_minutes"])
            effective_reset = (
                window["resets_at"] if window["resets_at"] is not None
                else item["observed_at_ms"] + EVIDENCE_HORIZON_MS
            )
            merged = windows.get(key)
            if merged is None:
                windows[key] = {**window, "resets_at": effective_reset}
                continue
            used = [value for value in (merged["used_percent"], window["used_percent"])
                    if value is not None]
            merged["used_percent"] = max(used) if used else None
            merged["resets_at"] = max(merged["resets_at"], effective_reset)
    last = records[-1]
    return headroom_record(
        provider=last["provider"],
        account_fingerprint=last["account_fingerprint"],
        windows=[windows[key] for key in sorted(windows, key=repr)],
        status=status,
        run_id=last["run_id"],
        phase=last["phase"],
        observed_at_ms=observed_at_ms,
    )


class _RecheckDenied(Exception):
    """An open's locked admission re-check denied; carries the decision to store."""

    def __init__(self, decision: dict[str, Any]) -> None:
        super().__init__(decision["reason_code"])
        self.decision = decision


class LearningLedger:
    """The only SQLite writer. Callers receive scoped repositories, never its connection."""

    def __init__(
        self,
        state: LearningState,
        *,
        client_capabilities: dict[str, dict[str, Any]] | None = None,
        admission_policy: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        # Admission settings come from the installed routing policy, read at
        # each decision; a caller-supplied policy is for tests only.
        self._admission_policy = admission_policy
        self._lock = threading.RLock()
        self._active_readers = 0
        self._client_capabilities = (
            client_capabilities
            if client_capabilities is not None
            else load_client_capabilities(
                Path(__file__).resolve().parent.parent / "config" / "learning-clients.json"
            )
        )
        self._collector_active = False
        self._writer_descriptor: int | None = None
        if fcntl is not None:
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    self.state.lock,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._writer_descriptor = descriptor
            except OSError as exc:
                if descriptor is not None:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                raise LedgerError("another learning writer owns this state root") from exc
        try:
            self._connection = self._connect()
            self._migrate()
            self._reconcile_managed_files()
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                with contextlib.suppress(sqlite3.Error):
                    connection.close()
            if self._writer_descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(self._writer_descriptor)
                self._writer_descriptor = None
            raise

    def _connect(self) -> sqlite3.Connection:
        if self.state.database.is_symlink():
            raise StateError("database path is unsafe")
        connection = sqlite3.connect(
            self.state.database,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        self.state.harden_database_files()
        return connection

    def _reconcile_managed_files(self) -> None:
        """Reconcile crash-orphaned files and registrations before serving work."""

        rows = self._connection.execute(
            "SELECT project_id,relative_path FROM stored_files"
        ).fetchall()
        registered: set[str] = set()
        with self._connection:
            for row in rows:
                relative = str(row["relative_path"])
                candidate = self.state.root / relative
                if candidate.is_symlink():
                    raise StateError("managed storage contains a symlink")
                if not candidate.is_file():
                    self._connection.execute(
                        "DELETE FROM stored_files WHERE project_id=? AND relative_path=?",
                        (str(row["project_id"]), relative),
                    )
                else:
                    registered.add(relative)
        for root in (self.state.artifacts, self.state.exports, self.state.backups):
            for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                current_path = Path(current)
                for directory in directories:
                    if (current_path / directory).is_symlink():
                        raise StateError("managed storage contains a symlink")
                for name in files:
                    candidate = current_path / name
                    if candidate.is_symlink():
                        raise StateError("managed storage contains a symlink")
                    relative = candidate.relative_to(self.state.root).as_posix()
                    if relative not in registered:
                        candidate.unlink()
            for current, directories, files in os.walk(root, topdown=False):
                path = Path(current)
                if path != root and not directories and not files:
                    with contextlib.suppress(OSError):
                        path.rmdir()

    def _require_schema_five_project_backups(self) -> None:
        """Refuse schema-6 migration until each active partition restores cleanly."""

        projects = self._connection.execute(
            "SELECT project_id FROM projects WHERE status='active' ORDER BY project_id"
        ).fetchall()
        for project in projects:
            project_id = str(project["project_id"])
            registrations = self._connection.execute(
                "SELECT file_id,relative_path,content_digest,expires_at_ms FROM stored_files "
                "WHERE project_id=? AND kind='backup' ORDER BY rowid DESC",
                (project_id,),
            ).fetchall()
            verified = False
            for registration in registrations:
                try:
                    expires_at_ms = registration["expires_at_ms"]
                    if (
                        expires_at_ms is not None
                        and int(expires_at_ms) <= time.time_ns() // 1_000_000
                    ):
                        continue
                    path = self.state.root / str(registration["relative_path"])
                    expected_parent = self.state.backups / project_id
                    _assert_no_symlink_components(path)
                    if path.parent != expected_parent or not path.is_file():
                        continue
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != str(
                        registration["content_digest"]
                    ):
                        continue
                    body, _, _ = _verify_backup_envelope(
                        raw,
                        expected_project_id=project_id,
                        expected_ledger_schema_version=5,
                    )
                    current = _snapshot_project_partition(
                        self._connection, project_id, ledger_schema_version=5
                    )
                    file_id = str(registration["file_id"])
                    current["tables"]["stored_files"] = [
                        row for row in current["tables"]["stored_files"]
                        if row["file_id"] != file_id
                    ]
                    current["counts"]["stored_files"] = len(
                        current["tables"]["stored_files"]
                    )
                    if canonical_json(current) != canonical_json(body):
                        continue
                except (LedgerError, OSError):
                    continue
                verified = True
                break
            if not verified:
                raise LedgerError(
                    "schema 6 migration requires a registered verified schema 5 "
                    "project backup"
                )

    def _migrate_five_to_six(self) -> None:
        """Apply the lifecycle migration as one rollback-safe write transaction."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(host_run_bindings)"
                ).fetchall()
            }
            additions = {
                "binding_state": "TEXT",
                "abandoned_reason": "TEXT",
                "observation_started_at_ms": "INTEGER",
                "observation_deadline_at_ms": "INTEGER",
                "last_progress_at_ms": "INTEGER",
                "deadline_observed": "INTEGER",
            }
            for name, sql_type in additions.items():
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE host_run_bindings ADD COLUMN {name} {sql_type}"
                    )
            self._connection.execute(
                "UPDATE host_run_bindings SET binding_state=CASE "
                "WHEN consumed_at_ms IS NULL THEN 'pending' ELSE 'terminal' END "
                "WHERE binding_state IS NULL"
            )
            self._connection.execute(
                "UPDATE host_run_bindings SET "
                "observation_started_at_ms=COALESCE(observation_started_at_ms,created_at_ms),"
                "last_progress_at_ms=COALESCE(last_progress_at_ms,created_at_ms),"
                "deadline_observed=COALESCE(deadline_observed,0)"
            )
            self._connection.execute("DROP INDEX IF EXISTS orchestration_status")
            self._connection.execute("DROP TRIGGER IF EXISTS cleanup_orchestrations_state")
            self._connection.execute("ALTER TABLE orchestrations RENAME TO orchestrations_v5")
            self._connection.execute(
                "CREATE TABLE orchestrations("
                "project_id TEXT NOT NULL,orchestration_id TEXT NOT NULL,"
                "handle_digest TEXT NOT NULL,client_version TEXT NOT NULL,"
                "status TEXT NOT NULL CHECK(status IN ('active','paused','completed',"
                "'failed','interrupted','cancelled')),started_at_ms INTEGER NOT NULL,"
                "ended_at_ms INTEGER,goal_tokens_used INTEGER,"
                "goal_time_used_seconds INTEGER,expires_at_ms INTEGER NOT NULL,"
                "PRIMARY KEY(project_id,orchestration_id),"
                "UNIQUE(project_id,handle_digest),FOREIGN KEY(project_id) "
                "REFERENCES projects(project_id) ON DELETE CASCADE)"
            )
            self._connection.execute(
                "INSERT INTO orchestrations SELECT * FROM orchestrations_v5"
            )
            self._connection.execute("DROP TABLE orchestrations_v5")
            self._connection.execute(
                "CREATE INDEX orchestration_status "
                "ON orchestrations(project_id,status,started_at_ms)"
            )
            self._connection.execute(
                "CREATE TRIGGER cleanup_orchestrations_state "
                "AFTER UPDATE OF status ON projects "
                "WHEN NEW.status='deleted' AND OLD.status<>'deleted' BEGIN "
                "DELETE FROM orchestrations WHERE project_id=NEW.project_id;"
                "DELETE FROM operational_qa WHERE project_id=NEW.project_id;"
                "DELETE FROM operational_check_receipts WHERE project_id=NEW.project_id;"
                "END"
            )
            self._connection.execute(
                "DROP TRIGGER IF EXISTS cleanup_host_binding_with_event"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS orchestration_checkpoints("
                "project_id TEXT NOT NULL,orchestration_id TEXT NOT NULL,"
                "sequence INTEGER NOT NULL,status TEXT NOT NULL,goal_tokens_used INTEGER,"
                "goal_time_used_seconds INTEGER,observed_at_ms INTEGER NOT NULL,"
                "PRIMARY KEY(project_id,orchestration_id,sequence),"
                "FOREIGN KEY(project_id,orchestration_id) REFERENCES "
                "orchestrations(project_id,orchestration_id) ON DELETE CASCADE)"
            )
            self._connection.execute(
                "INSERT INTO orchestration_checkpoints("
                "project_id,orchestration_id,sequence,status,goal_tokens_used,"
                "goal_time_used_seconds,observed_at_ms) "
                "SELECT project_id,orchestration_id,1,status,goal_tokens_used,"
                "goal_time_used_seconds,COALESCE(ended_at_ms,started_at_ms) "
                "FROM orchestrations"
            )
            self._connection.execute("PRAGMA user_version = 6")
            self._connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current > LEDGER_SCHEMA_VERSION:
            raise LedgerError("ledger schema is newer than this package")
        if current == 5:
            self._require_schema_five_project_backups()
            self._migrate_five_to_six()
            self.state.harden_database_files()
            return
        had_hook_ingestion = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_ingestion'"
        ).fetchone() is not None
        if current == 0:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        canonical_root TEXT,
                        root_digest TEXT,
                        status TEXT NOT NULL CHECK(status IN ('active','deleted')),
                        created_at_ms INTEGER NOT NULL,
                        deleted_at_ms INTEGER
                    );
                    CREATE TABLE sessions (
                        project_id TEXT NOT NULL,
                        session_digest TEXT NOT NULL,
                        producer TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, session_digest, producer),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS run_provenance (
                        project_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        producer TEXT NOT NULL,
                        client_version TEXT NOT NULL,
                        session_digest TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, run_id, producer),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE records (
                        project_id TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        record_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        source_sequence INTEGER NOT NULL,
                        observed_at_ms INTEGER NOT NULL,
                        ingested_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER,
                        content BLOB NOT NULL,
                        content_digest TEXT NOT NULL,
                        late INTEGER NOT NULL CHECK(late IN (0,1)),
                        PRIMARY KEY(project_id, record_id),
                        UNIQUE(project_id, source_sequence),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE record_links (
                        project_id TEXT NOT NULL,
                        from_record_id TEXT NOT NULL,
                        to_record_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        PRIMARY KEY(project_id, from_record_id, to_record_id, relation),
                        FOREIGN KEY(project_id, from_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, to_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE
                    );
                    CREATE TABLE stored_files (
                        project_id TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('artifact','export','backup')),
                        relative_path TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        source_record_id TEXT,
                        expires_at_ms INTEGER,
                        PRIMARY KEY(project_id, file_id),
                        UNIQUE(project_id, relative_path),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, source_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE
                    );
                    CREATE TABLE collector_health (
                        project_id TEXT NOT NULL,
                        code TEXT NOT NULL,
                        count INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, code),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE tombstones (
                        project_id TEXT NOT NULL,
                        deletion_id TEXT NOT NULL,
                        deleted_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, deletion_id)
                    );
                    CREATE TABLE consumed_grants (
                        project_id TEXT NOT NULL,
                        grant_id TEXT NOT NULL,
                        promotion_record_id TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL,
                        action TEXT NOT NULL,
                        consumed_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, grant_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, grant_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, promotion_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE
                    );
                    CREATE TABLE owner_attestations (
                        project_id TEXT NOT NULL,
                        subject_record_id TEXT NOT NULL,
                        signature BLOB NOT NULL,
                        attested_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, subject_record_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, subject_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE
                    );
                    CREATE TABLE repair_receipts (
                        project_id TEXT NOT NULL,
                        receipt_id TEXT NOT NULL,
                        assignment_digest TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        issued_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER NOT NULL,
                        consumed_at_ms INTEGER,
                        run_id TEXT,
                        PRIMARY KEY(project_id, receipt_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    PRAGMA user_version = 4;
                    """
                )
        elif current == 1:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE owner_attestations (
                        project_id TEXT NOT NULL,
                        subject_record_id TEXT NOT NULL,
                        signature BLOB NOT NULL,
                        attested_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, subject_record_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                        FOREIGN KEY(project_id, subject_record_id)
                            REFERENCES records(project_id, record_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS run_provenance (
                        project_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        producer TEXT NOT NULL,
                        client_version TEXT NOT NULL,
                        session_digest TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, run_id, producer),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE repair_receipts (
                        project_id TEXT NOT NULL,
                        receipt_id TEXT NOT NULL,
                        assignment_digest TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        issued_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER NOT NULL,
                        consumed_at_ms INTEGER,
                        run_id TEXT,
                        PRIMARY KEY(project_id, receipt_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    PRAGMA user_version = 4;
                    """
                )
        elif current == 2:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE run_provenance (
                        project_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        producer TEXT NOT NULL,
                        client_version TEXT NOT NULL,
                        session_digest TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, run_id, producer),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE repair_receipts (
                        project_id TEXT NOT NULL,
                        receipt_id TEXT NOT NULL,
                        assignment_digest TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        issued_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER NOT NULL,
                        consumed_at_ms INTEGER,
                        run_id TEXT,
                        PRIMARY KEY(project_id, receipt_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    PRAGMA user_version = 4;
                    """
                )
        elif current == 3:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE repair_receipts (
                        project_id TEXT NOT NULL,
                        receipt_id TEXT NOT NULL,
                        assignment_digest TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        issued_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER NOT NULL,
                        consumed_at_ms INTEGER,
                        run_id TEXT,
                        PRIMARY KEY(project_id, receipt_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    PRAGMA user_version = 4;
                    """
                )
        # These tables are additive and deliberately keep user_version at 4 so a
        # registered 0.3.0 project can still cold-start the shared collector.
        if current <= LEDGER_SCHEMA_VERSION:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS host_run_bindings (
                        project_id TEXT NOT NULL,
                        handle_digest TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        assignment_digest TEXT NOT NULL,
                        task_digest TEXT NOT NULL,
                        task_id_digest TEXT,
                        route_digest TEXT NOT NULL,
                        configuration_digest TEXT NOT NULL,
                        client_version TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER NOT NULL,
                        consumed_at_ms INTEGER,
                        PRIMARY KEY(project_id, handle_digest),
                        UNIQUE(project_id, run_id),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS hook_ingestion (
                        project_id TEXT NOT NULL,
                        event_code TEXT NOT NULL CHECK(event_code IN (
                            'session_started','session_ended','turn_started',
                            'turn_stop_observed','turn_interrupted','subagent_started',
                            'subagent_stop_observed','tool_started','tool_output_observed'
                        )),
                        accepted_count INTEGER NOT NULL CHECK(accepted_count>=0),
                        last_accepted_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, event_code),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS additive_migrations (
                        feature_key TEXT PRIMARY KEY
                    );
                    CREATE INDEX IF NOT EXISTS hook_event_latest
                    ON records(
                        project_id,
                        json_extract(CAST(content AS TEXT),'$.event_code'),
                        ingested_at_ms DESC
                    )
                    WHERE source='hook' AND record_type='event';
                    CREATE TRIGGER IF NOT EXISTS aggregate_hook_event_insert
                    AFTER INSERT ON records
                    WHEN NEW.source='hook' AND NEW.record_type='event'
                         AND json_extract(CAST(NEW.content AS TEXT),'$.event_code') IN (
                            'session_started','session_ended','turn_started',
                            'turn_stop_observed','turn_interrupted','subagent_started',
                            'subagent_stop_observed','tool_started','tool_output_observed'
                         )
                    BEGIN
                        INSERT INTO hook_ingestion VALUES(
                            NEW.project_id,
                            json_extract(CAST(NEW.content AS TEXT),'$.event_code'),
                            1,
                            NEW.ingested_at_ms
                        )
                        ON CONFLICT(project_id,event_code) DO UPDATE SET
                            accepted_count=accepted_count+1,
                            last_accepted_at_ms=MAX(
                                last_accepted_at_ms,excluded.last_accepted_at_ms
                            );
                    END;
                    CREATE TRIGGER IF NOT EXISTS aggregate_hook_event_delete
                    AFTER DELETE ON records
                    WHEN OLD.source='hook' AND OLD.record_type='event'
                         AND json_extract(CAST(OLD.content AS TEXT),'$.event_code') IN (
                            'session_started','session_ended','turn_started',
                            'turn_stop_observed','turn_interrupted','subagent_started',
                            'subagent_stop_observed','tool_started','tool_output_observed'
                         )
                    BEGIN
                        UPDATE hook_ingestion
                        SET accepted_count=accepted_count-1,
                            last_accepted_at_ms=CASE
                                WHEN OLD.ingested_at_ms<last_accepted_at_ms
                                THEN last_accepted_at_ms
                                ELSE COALESCE(
                                    (
                                        SELECT MAX(ingested_at_ms) FROM records
                                        WHERE project_id=OLD.project_id
                                          AND source='hook' AND record_type='event'
                                          AND json_extract(
                                              CAST(content AS TEXT),'$.event_code'
                                          )=json_extract(
                                              CAST(OLD.content AS TEXT),'$.event_code'
                                          )
                                    ),
                                    0
                                )
                            END
                        WHERE project_id=OLD.project_id
                          AND event_code=json_extract(
                              CAST(OLD.content AS TEXT),'$.event_code'
                          );
                        DELETE FROM hook_ingestion
                        WHERE project_id=OLD.project_id AND accepted_count<=0;
                    END;
                    CREATE TRIGGER IF NOT EXISTS cleanup_additive_learning_state
                    AFTER UPDATE OF status ON projects
                    WHEN NEW.status='deleted' AND OLD.status<>'deleted'
                    BEGIN
                        DELETE FROM host_run_bindings WHERE project_id=NEW.project_id;
                        DELETE FROM hook_ingestion WHERE project_id=NEW.project_id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS cleanup_host_binding_with_event
                    AFTER DELETE ON records
                    WHEN OLD.source='host' AND OLD.record_type='event'
                    BEGIN
                        DELETE FROM host_run_bindings
                        WHERE project_id=OLD.project_id
                          AND run_id=json_extract(
                              CAST(OLD.content AS TEXT),'$.run_id'
                          );
                    END;
                    """
                )
                migration_complete = had_hook_ingestion and self._connection.execute(
                    "SELECT 1 FROM additive_migrations "
                    "WHERE feature_key='hook_ingestion_v1'"
                ).fetchone() is not None
                if not migration_complete:
                    self._connection.execute("DELETE FROM hook_ingestion")
                    aggregates: dict[tuple[str, str], tuple[int, int]] = {}
                    rows = self._connection.execute(
                        "SELECT project_id,content,ingested_at_ms FROM records "
                        "WHERE source='hook' AND record_type='event'"
                    ).fetchall()
                    for row in rows:
                        event_code = json.loads(bytes(row["content"])).get("event_code")
                        if event_code not in HOOK_EVENT_CODES:
                            continue
                        key = (str(row["project_id"]), event_code)
                        prior_count, prior_latest = aggregates.get(key, (0, 0))
                        aggregates[key] = (
                            prior_count + 1,
                            max(prior_latest, int(row["ingested_at_ms"])),
                        )
                    self._connection.executemany(
                        "INSERT INTO hook_ingestion VALUES(?,?,?,?)",
                        [
                            (project_id, event_code, count, latest)
                            for (project_id, event_code), (count, latest)
                            in aggregates.items()
                        ],
                    )
                    self._connection.execute(
                        "INSERT OR REPLACE INTO additive_migrations "
                        "VALUES('hook_ingestion_v1')"
                    )
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orchestrations (
                    project_id TEXT NOT NULL,
                    orchestration_id TEXT NOT NULL,
                    handle_digest TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'active','completed','failed','interrupted','cancelled'
                    )),
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    goal_tokens_used INTEGER,
                    goal_time_used_seconds INTEGER,
                    expires_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(project_id, orchestration_id),
                    UNIQUE(project_id, handle_digest),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS orchestration_status
                ON orchestrations(project_id,status,started_at_ms);
                CREATE TABLE IF NOT EXISTS operational_qa (
                    project_id TEXT NOT NULL,
                    qa_id TEXT NOT NULL,
                    qa_digest TEXT NOT NULL,
                    host_outcome_digest TEXT NOT NULL,
                    verifier_host_outcome_digest TEXT NOT NULL,
                    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
                    observed_at_ms INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    PRIMARY KEY(project_id, qa_id),
                    UNIQUE(project_id, host_outcome_digest),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS operational_check_receipts (
                    project_id TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    report_digest TEXT NOT NULL,
                    host_outcome_digest TEXT NOT NULL,
                    verifier_host_outcome_digest TEXT NOT NULL,
                    orchestration_id TEXT NOT NULL,
                    issued_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    consumed_at_ms INTEGER,
                    report BLOB NOT NULL,
                    PRIMARY KEY(project_id, receipt_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );
                CREATE TRIGGER IF NOT EXISTS cleanup_orchestrations_state
                AFTER UPDATE OF status ON projects
                WHEN NEW.status='deleted' AND OLD.status<>'deleted'
                BEGIN
                    DELETE FROM orchestrations WHERE project_id=NEW.project_id;
                    DELETE FROM operational_qa WHERE project_id=NEW.project_id;
                    DELETE FROM operational_check_receipts
                    WHERE project_id=NEW.project_id;
                END;
                CREATE TRIGGER IF NOT EXISTS cleanup_operational_qa_with_outcome
                AFTER DELETE ON records
                WHEN OLD.source='host' AND OLD.record_type='outcome'
                BEGIN
                    DELETE FROM operational_qa
                    WHERE project_id=OLD.project_id
                      AND (
                          host_outcome_digest=OLD.content_digest
                          OR verifier_host_outcome_digest=OLD.content_digest
                      );
                    DELETE FROM operational_check_receipts
                    WHERE project_id=OLD.project_id
                      AND (
                          host_outcome_digest=OLD.content_digest
                          OR verifier_host_outcome_digest=OLD.content_digest
                      );
                END;
                """
            )
            orchestration_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(orchestrations)"
                ).fetchall()
            }
            if "expires_at_ms" not in orchestration_columns:
                self._connection.execute(
                    "ALTER TABLE orchestrations ADD COLUMN expires_at_ms INTEGER"
                )
                self._connection.execute(
                    "UPDATE orchestrations SET expires_at_ms=started_at_ms+? "
                    "WHERE expires_at_ms IS NULL",
                    (DEFAULT_RETENTION_MS,),
                )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(host_run_bindings)"
                ).fetchall()
            }
            additions = {
                "orchestration_id": "TEXT",
                "native_assignment_id": "TEXT",
                "agent_type": "TEXT",
                "usage_complete": "INTEGER",
                "task_id_digest": "TEXT",
                "binding_state": "TEXT",
                "abandoned_reason": "TEXT",
                "observation_started_at_ms": "INTEGER",
                "observation_deadline_at_ms": "INTEGER",
                "last_progress_at_ms": "INTEGER",
                "deadline_observed": "INTEGER",
            }
            for name, sql_type in additions.items():
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE host_run_bindings ADD COLUMN {name} {sql_type}"
                    )
            self._connection.execute(
                "UPDATE host_run_bindings SET binding_state=CASE "
                "WHEN consumed_at_ms IS NULL THEN 'pending' ELSE 'terminal' END "
                "WHERE binding_state IS NULL"
            )
            self._connection.execute(
                "UPDATE host_run_bindings SET "
                "observation_started_at_ms=COALESCE(observation_started_at_ms,created_at_ms),"
                "last_progress_at_ms=COALESCE(last_progress_at_ms,created_at_ms),"
                "deadline_observed=COALESCE(deadline_observed,0)"
            )
            if current < 6:
                self._connection.executescript(
                    """
                    DROP TABLE IF EXISTS orchestration_checkpoints;
                    DROP INDEX IF EXISTS orchestration_status;
                    DROP TRIGGER IF EXISTS cleanup_orchestrations_state;
                    ALTER TABLE orchestrations RENAME TO orchestrations_v5;
                    CREATE TABLE orchestrations (
                        project_id TEXT NOT NULL,
                        orchestration_id TEXT NOT NULL,
                        handle_digest TEXT NOT NULL,
                        client_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'active','paused','completed','failed','interrupted','cancelled'
                        )),
                        started_at_ms INTEGER NOT NULL,
                        ended_at_ms INTEGER,
                        goal_tokens_used INTEGER,
                        goal_time_used_seconds INTEGER,
                        expires_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(project_id, orchestration_id),
                        UNIQUE(project_id, handle_digest),
                        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                    );
                    INSERT INTO orchestrations SELECT * FROM orchestrations_v5;
                    DROP TABLE orchestrations_v5;
                    CREATE INDEX orchestration_status
                    ON orchestrations(project_id,status,started_at_ms);
                    CREATE TRIGGER cleanup_orchestrations_state
                    AFTER UPDATE OF status ON projects
                    WHEN NEW.status='deleted' AND OLD.status<>'deleted'
                    BEGIN
                        DELETE FROM orchestrations WHERE project_id=NEW.project_id;
                        DELETE FROM operational_qa WHERE project_id=NEW.project_id;
                        DELETE FROM operational_check_receipts
                        WHERE project_id=NEW.project_id;
                    END;
                    DROP TRIGGER IF EXISTS cleanup_host_binding_with_event;
                    """
                )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orchestration_checkpoints (
                    project_id TEXT NOT NULL,
                    orchestration_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    goal_tokens_used INTEGER,
                    goal_time_used_seconds INTEGER,
                    observed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(project_id, orchestration_id, sequence),
                    FOREIGN KEY(project_id, orchestration_id)
                        REFERENCES orchestrations(project_id, orchestration_id)
                        ON DELETE CASCADE
                );
                """
            )
            if current < 6:
                self._connection.execute(
                    "INSERT INTO orchestration_checkpoints("
                    "project_id,orchestration_id,sequence,status,goal_tokens_used,"
                    "goal_time_used_seconds,observed_at_ms) "
                    "SELECT project_id,orchestration_id,1,status,goal_tokens_used,"
                    "goal_time_used_seconds,COALESCE(ended_at_ms,started_at_ms) "
                    "FROM orchestrations"
                )
            self._connection.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
        self.state.harden_database_files()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            descriptor = self._writer_descriptor
            self._writer_descriptor = None
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def register_project(self, project_root: Path, *, now_ms: int | None = None) -> str:
        root = _canonical_project_root(project_root)
        root_text = str(root)
        digest = keyed_digest(self.state.digest_key(), root_text)
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock:
            row = self._connection.execute(
                "SELECT project_id FROM projects WHERE root_digest=? AND status='active'",
                (digest,),
            ).fetchone()
            if row:
                return str(row["project_id"])
            project_id = f"prj_{secrets.token_hex(16)}"
            self._connection.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,NULL)",
                (project_id, root_text, digest, "active", timestamp),
            )
            return project_id

    def project_for_root(self, project_root: Path) -> str:
        root = _canonical_project_root(project_root)
        digest = keyed_digest(self.state.digest_key(), str(root))
        with self._lock:
            row = self._connection.execute(
                "SELECT project_id FROM projects WHERE root_digest=? AND status='active'",
                (digest,),
            ).fetchone()
        if not row:
            raise ProjectNotRegisteredError("project is not registered")
        return str(row["project_id"])

    def issue_result_repair_receipt(
        self,
        project_id: str,
        assignment_digest: str,
        reason_code: str,
        *,
        now_ms: int | None = None,
    ) -> str:
        """Issue one opaque, content-free receipt for a validator rejection."""

        if (
            not isinstance(assignment_digest, str)
            or len(assignment_digest) != 64
            or any(character not in "0123456789abcdef" for character in assignment_digest)
            or not isinstance(reason_code, str)
            or reason_code not in RESULT_VALIDATION_REASON_CODES
        ):
            raise LedgerError("repair receipt request is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        receipt_id = f"rpr_{secrets.token_hex(24)}"
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM repair_receipts WHERE project_id=? AND expires_at_ms<=?",
                (project_id, timestamp),
            )
            pending = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM repair_receipts WHERE project_id=? "
                    "AND consumed_at_ms IS NULL",
                    (project_id,),
                ).fetchone()[0]
            )
            if pending >= MAX_PENDING_REPAIR_RECEIPTS:
                raise LedgerError("pending repair receipt capacity is exhausted")
            self._connection.execute(
                "INSERT INTO repair_receipts VALUES(?,?,?,?,?,?,NULL,NULL)",
                (
                    project_id,
                    receipt_id,
                    assignment_digest,
                    reason_code,
                    timestamp,
                    timestamp + REPAIR_RECEIPT_TTL_MS,
                ),
            )
        return receipt_id

    def bind_session(
        self,
        project_root: Path,
        session_id: str,
        producer: str,
        client_version: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[str, str, str]:
        if producer not in {"hook", "app_server"}:
            raise LedgerError("collector producer is not authorized")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 1024:
            raise LedgerError("session identifier is invalid")
        if not isinstance(client_version, str) or not client_version or len(client_version) > 160:
            raise LedgerError("client version is invalid")
        project_id = self.project_for_root(project_root)
        session_digest = keyed_digest(self.state.digest_key(), session_id)
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        root_run_id = external_id(
            self.state.digest_key(), "run", f"{client_version}\x00{session_id}\x00root"
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO sessions VALUES(?,?,?,?)",
                (project_id, session_digest, producer, timestamp),
            )
            new_run = self._connection.execute(
                "INSERT OR IGNORE INTO run_provenance VALUES(?,?,?,?,?,?)",
                (
                    project_id, root_run_id, producer, client_version,
                    session_digest, timestamp,
                ),
            ).rowcount == 1
        if new_run:
            self._admit_session_run(project_id, root_run_id)
        return project_id, session_digest, root_run_id

    def _admit_session_run(self, project_id: str, run_id: str) -> None:
        """Decide admission once for a collector-bound Codex session run.

        These sessions never pass through open_host_run, so their admission is
        taken when they first bind. Only a pass is stored: a denied session can
        still record events, but its result can never be committed.
        """

        with self._lock, self._connection:
            # The ledger's clock is read under the lock that orders decisions.
            timestamp = self._clock_ms()
            decision = self._evaluate_admission_locked(
                project_id, provider="codex", environ=os.environ, timestamp=timestamp
            )
            if decision["decision"] == "pass":
                self._store_admission_locked(
                    project_id, decision, run_id=run_id, timestamp=timestamp
                )
        self.state.harden_database_files()

    def append(
        self,
        project_id: str,
        record: dict[str, Any],
        *,
        producer: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        try:
            checked = validate_learning_record(record)
        except ContractError as exc:
            raise LedgerError("record admission rejected") from exc
        allowed = PRODUCER_AUTHORITY.get(producer, set())
        if checked["record_type"] not in allowed:
            raise LedgerError("producer is not authorized for record type")
        if (
            checked["record_type"] == "candidate"
            and checked.get("eligibility_receipt") is None
        ):
            raise LedgerError("candidate lacks a verified eligibility receipt")
        if producer == "owner":
            raise LedgerError("owner records require detached signature verification")
        if checked["record_type"] == "outcome" and producer in {"evaluator", "verifier"}:
            raise LedgerError("outcomes require atomic host result admission")
        if checked["record_type"] in {"qa_evidence", "evidence_review"}:
            raise LedgerError("verification evidence requires controlled admission")
        if checked["record_type"] in PROVIDER_OBSERVATION_TYPES | {
            "admission_decision", "billing_attestation", "dispatch_intent",
            "host_run_profile", "probe_admission",
        }:
            raise LedgerError("provider evidence requires ledger-run observation")
        if (
            checked["record_type"] == "promotion"
            and checked["action"] in {"canary_started", "promoted"}
        ):
            raise LedgerError("authorized transition requires atomic grant consumption")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            self._validate_evidence_links_locked(project_id, checked)
            result = self._append_checked_locked(
                project_id, checked, producer=producer, timestamp=timestamp
            )
        self.state.harden_database_files()
        return result

    def _validate_candidate_eligibility_locked(
        self, project_id: str, candidate: dict[str, Any]
    ) -> None:
        """Derive candidate eligibility from persisted admitted outcomes."""

        evidence = candidate["evidence_digests"]
        receipt = candidate["eligibility_receipt"]
        placeholders = ",".join("?" for _ in evidence)
        rows = self._connection.execute(
            "SELECT content_digest,record_type,content,source_sequence FROM records "
            f"WHERE project_id=? AND content_digest IN ({placeholders})",
            (project_id, *evidence),
        ).fetchall()
        if len(rows) != len(evidence) or any(
            row["record_type"] != "outcome" for row in rows
        ):
            raise LedgerError("candidate evidence is not a complete outcome cohort")
        outcomes = [
            (
                json.loads(bytes(row["content"])),
                str(row["content_digest"]),
                int(row["source_sequence"]),
            )
            for row in rows
        ]
        accepted_operational_qa = {
            str(row["qa_digest"])
            for row in self._connection.execute(
                "SELECT qa_digest FROM operational_qa "
                "WHERE project_id=? AND accepted=1",
                (project_id,),
            ).fetchall()
        }
        accepted_formal_qa = {
            str(row["content_digest"])
            for row in self._connection.execute(
                "SELECT records.content_digest FROM records "
                "JOIN owner_attestations ON "
                "owner_attestations.project_id=records.project_id AND "
                "owner_attestations.subject_record_id=records.record_id "
                "WHERE records.project_id=? AND records.record_type='qa_evidence' "
                "AND json_extract(CAST(records.content AS TEXT),'$.accepted')=1",
                (project_id,),
            ).fetchall()
        }
        accepted_qa = accepted_operational_qa | accepted_formal_qa
        required_usage = {
            "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
            "model_calls", "tool_calls", "latency_ms",
        }
        for outcome, _, _ in outcomes:
            sources = outcome.get("usage_sources")
            if (
                outcome["task_slice"] != candidate["task_slice"]
                or outcome["accepted"] is not True
                or outcome.get("execution_state") != "completed"
                or outcome.get("admission_state") != "accepted"
                or outcome.get("semantic_verdict")
                not in {"pass", "ready", "ready-with-corrections"}
                or not isinstance(outcome.get("retry_attempt"), int)
                or outcome["verification_required"] is not True
                or outcome["verifier_digest"] is None
                or outcome["verifier_disagreement"] is not False
                or outcome["deterministic_pass"] is not True
                or outcome["security_pass"] is not True
                or outcome["required_criteria_passed"]
                != outcome["required_criteria_total"]
                or outcome["coverage"] != "end_to_end_complete"
                or not (set(outcome["evidence_digests"]) & accepted_qa)
                or any(outcome["usage"][field] is None for field in required_usage)
                or not isinstance(sources, dict)
                or any(
                    sources.get(field, {}).get("source") == "unavailable"
                    or sources.get(field, {}).get("coverage")
                    != "end_to_end_complete"
                    for field in required_usage
                )
            ):
                raise LedgerError("candidate evidence fails a verified eligibility gate")
        versions = {
            (
                outcome["package_digest"], outcome["policy_digest"],
                outcome["client_digest"], outcome["task_slice"],
            )
            for outcome, _, _ in outcomes
        }
        if len(versions) != 1:
            raise LedgerError("candidate evidence is version or task-mix confounded")
        configuration_fields = (
            "model", "effort", "role", "topology", "max_parallel",
            "verification_required", "verifier_digest", "bounds_digest",
            "package_digest", "policy_digest", "agent_digest", "skill_digests",
            "prompt_digest", "client_digest",
        )

        def configuration(outcome: dict[str, Any]) -> dict[str, Any]:
            return {field: outcome[field] for field in configuration_fields}

        champion = [
            outcome for outcome, _, _ in outcomes
            if configuration(outcome) == candidate["champion_configuration"]
        ]
        challenger = [
            outcome for outcome, _, _ in outcomes
            if configuration(outcome) == candidate["candidate_configuration"]
        ]
        matched_fixtures = {
            outcome["fixture_digest"] for outcome in champion
        } & {
            outcome["fixture_digest"] for outcome in challenger
        }
        if (
            len(champion) + len(challenger) != len(outcomes)
            or len(matched_fixtures) < 20
            or any(
                sum(item["fixture_digest"] == fixture for item in cohort) != 1
                for fixture in matched_fixtures
                for cohort in (champion, challenger)
            )
            or any(
                item["fixture_digest"] not in matched_fixtures
                for item, _, _ in outcomes
            )
            or receipt["independent_case_count"] != len(matched_fixtures)
        ):
            raise LedgerError("candidate evidence lacks a matched task cohort")

        # Retry claims are only authoritative when their complete ancestry already
        # exists in this project. Multiple records for one assignment are expected
        # while an unverified host outcome is superseded by accepted QA, but every
        # version must agree on its lineage and task fixture.
        lineage_rows = self._connection.execute(
            "SELECT content,source_sequence FROM records WHERE project_id=? "
            "AND record_type='outcome' ORDER BY source_sequence",
            (project_id,),
        ).fetchall()
        lineage: dict[str, list[tuple[dict[str, Any], int]]] = {}
        for row in lineage_rows:
            outcome = json.loads(bytes(row["content"]))
            lineage.setdefault(str(outcome["assignment_id"]), []).append(
                (outcome, int(row["source_sequence"]))
            )
        for assignment_outcomes in lineage.values():
            claims = {
                (
                    item.get("retry_attempt", 1),
                    item.get("retry_of_assignment_id"),
                    item["task_slice"],
                    item["fixture_digest"],
                )
                for item, _ in assignment_outcomes
            }
            if len(claims) != 1:
                raise LedgerError("persisted retry lineage is internally inconsistent")

        def retry_root(outcome: dict[str, Any], source_sequence: int) -> str:
            current = outcome
            current_sequence = source_sequence
            seen: set[str] = set()
            while True:
                assignment_id = str(current["assignment_id"])
                if assignment_id in seen:
                    raise LedgerError("candidate retry lineage is cyclic")
                seen.add(assignment_id)
                predecessor = current.get("retry_of_assignment_id")
                attempt = current.get("retry_attempt")
                if predecessor is None:
                    if attempt != 1:
                        raise LedgerError("candidate retry lineage is incomplete")
                    return assignment_id
                prior = [
                    item
                    for item in lineage.get(str(predecessor), [])
                    if item[1] < current_sequence
                ]
                if not prior:
                    raise LedgerError(
                        "candidate retry predecessor is not an earlier persisted outcome"
                    )
                parent, parent_sequence = prior[-1]
                if (
                    parent["task_slice"] != current["task_slice"]
                    or parent["fixture_digest"] != current["fixture_digest"]
                    or parent.get("retry_attempt", 1) != attempt - 1
                ):
                    raise LedgerError("candidate retry predecessor changes task lineage")
                current = parent
                current_sequence = parent_sequence

        roots = {
            retry_root(outcome, source_sequence)
            for outcome, _, source_sequence in outcomes
        }
        if len(roots) != len(outcomes) or len(roots) < 40:
            raise LedgerError("candidate evidence is not independently retry-deduplicated")

    def open_orchestration(
        self,
        project_id: str,
        *,
        client_version: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Open one goal-scoped statistics boundary."""

        capability = self._client_capabilities.get(client_version)
        if capability is None or "host" not in capability.get("producers", []):
            raise LedgerError("orchestration client is not authorized")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        handle = f"orc_{secrets.token_hex(24)}"
        handle_digest = keyed_digest(self.state.digest_key(), handle)
        orchestration_id = external_id(self.state.digest_key(), "orc", handle)
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            self._connection.execute(
                "INSERT INTO orchestrations("
                "project_id,orchestration_id,handle_digest,client_version,status,"
                "started_at_ms,ended_at_ms,goal_tokens_used,goal_time_used_seconds,"
                "expires_at_ms) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)",
                (
                    project_id,
                    orchestration_id,
                    handle_digest,
                    client_version,
                    "active",
                    timestamp,
                    timestamp + DEFAULT_RETENTION_MS,
                ),
            )
            self._append_orchestration_checkpoint_locked(
                project_id,
                orchestration_id,
                status="active",
                goal_tokens_used=None,
                goal_time_used_seconds=None,
                observed_at_ms=timestamp,
            )
        self.state.harden_database_files()
        return {
            "orchestration_handle": handle,
            "orchestration_id": orchestration_id,
            "status": "active",
            "started_at_ms": timestamp,
        }

    def _orchestration_for_handle_locked(
        self, project_id: str, handle: str
    ) -> sqlite3.Row:
        if not isinstance(handle, str) or not re.fullmatch(r"orc_[0-9a-f]{48}", handle):
            raise LedgerError("orchestration handle is invalid")
        row = self._connection.execute(
            "SELECT * FROM orchestrations WHERE project_id=? AND handle_digest=?",
            (project_id, keyed_digest(self.state.digest_key(), handle)),
        ).fetchone()
        if row is None:
            raise LedgerError("orchestration handle is unavailable")
        return row

    def _append_orchestration_checkpoint_locked(
        self,
        project_id: str,
        orchestration_id: str,
        *,
        status: str,
        goal_tokens_used: int | None,
        goal_time_used_seconds: int | None,
        observed_at_ms: int,
    ) -> None:
        prior = self._connection.execute(
            "SELECT COALESCE(MAX(sequence),0),MAX(observed_at_ms) "
            "FROM orchestration_checkpoints WHERE project_id=? AND orchestration_id=?",
            (project_id, orchestration_id),
        ).fetchone()
        sequence = int(prior[0]) + 1
        if prior[1] is not None and observed_at_ms < int(prior[1]):
            raise LedgerError("orchestration checkpoint time cannot move backward")
        self._connection.execute(
            "INSERT INTO orchestration_checkpoints VALUES(?,?,?,?,?,?,?)",
            (
                project_id,
                orchestration_id,
                sequence,
                status,
                goal_tokens_used,
                goal_time_used_seconds,
                observed_at_ms,
            ),
        )

    def set_orchestration_paused(
        self,
        project_id: str,
        *,
        orchestration_handle: str,
        paused: bool,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist an explicit pause/resume transition without resetting history."""

        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            row = self._orchestration_for_handle_locked(project_id, orchestration_handle)
            expected = "active" if paused else "paused"
            target = "paused" if paused else "active"
            if row["status"] != expected:
                raise LedgerError(f"orchestration must be {expected} for this transition")
            self._connection.execute(
                "UPDATE orchestrations SET status=? WHERE project_id=? "
                "AND orchestration_id=? AND status=?",
                (target, project_id, str(row["orchestration_id"]), expected),
            )
            self._append_orchestration_checkpoint_locked(
                project_id,
                str(row["orchestration_id"]),
                status=target,
                goal_tokens_used=row["goal_tokens_used"],
                goal_time_used_seconds=row["goal_time_used_seconds"],
                observed_at_ms=timestamp,
            )
            orchestration_id = str(row["orchestration_id"])
        self.state.harden_database_files()
        return self.orchestration_report(project_id, orchestration_id)

    def close_orchestration(
        self,
        project_id: str,
        *,
        orchestration_handle: str,
        status: str,
        goal_tokens_used: int | None = None,
        goal_time_used_seconds: int | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Close one boundary and return its exact aggregate report."""

        if status not in {"completed", "failed", "interrupted", "cancelled"}:
            raise LedgerError("orchestration terminal status is invalid")
        for value, label in (
            (goal_tokens_used, "goal token usage"),
            (goal_time_used_seconds, "goal elapsed time"),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise LedgerError(f"{label} is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            row = self._orchestration_for_handle_locked(
                project_id, orchestration_handle
            )
            if row["status"] not in {"active", "paused"}:
                raise LedgerError("orchestration is already closed")
            if timestamp < int(row["started_at_ms"]):
                raise LedgerError("orchestration close time is invalid")
            unfinished = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=? "
                    "AND orchestration_id=? AND binding_state='pending'",
                    (project_id, str(row["orchestration_id"])),
                ).fetchone()[0]
            )
            if unfinished:
                raise LedgerError("orchestration has unfinished host runs")
            resolved_goal_tokens = (
                goal_tokens_used
                if goal_tokens_used is not None
                else row["goal_tokens_used"]
            )
            resolved_goal_time = (
                goal_time_used_seconds
                if goal_time_used_seconds is not None
                else row["goal_time_used_seconds"]
            )
            for prior, current, label in (
                (row["goal_tokens_used"], resolved_goal_tokens, "goal token usage"),
                (
                    row["goal_time_used_seconds"],
                    resolved_goal_time,
                    "goal elapsed time",
                ),
            ):
                if (
                    prior is not None
                    and current is not None
                    and int(current) < int(prior)
                ):
                    raise LedgerError(f"{label} cannot decrease")
            self._connection.execute(
                "UPDATE orchestrations SET status=?,ended_at_ms=?,"
                "goal_tokens_used=?,goal_time_used_seconds=? "
                "WHERE project_id=? AND orchestration_id=? AND status=?",
                (
                    status,
                    timestamp,
                    resolved_goal_tokens,
                    resolved_goal_time,
                    project_id,
                    str(row["orchestration_id"]),
                    str(row["status"]),
                ),
            )
            orchestration_id = str(row["orchestration_id"])
            self._append_orchestration_checkpoint_locked(
                project_id,
                orchestration_id,
                status=status,
                goal_tokens_used=resolved_goal_tokens,
                goal_time_used_seconds=resolved_goal_time,
                observed_at_ms=timestamp,
            )
        self.state.harden_database_files()
        return self.orchestration_report(project_id, orchestration_id)

    def checkpoint_orchestration(
        self,
        project_id: str,
        *,
        orchestration_handle: str,
        goal_tokens_used: int,
        goal_time_used_seconds: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist one exact, monotonic native-goal usage checkpoint."""

        for value, label in (
            (goal_tokens_used, "goal token usage"),
            (goal_time_used_seconds, "goal elapsed time"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LedgerError(f"{label} is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            row = self._orchestration_for_handle_locked(
                project_id, orchestration_handle
            )
            if row["status"] not in {"active", "paused"}:
                raise LedgerError("orchestration is already closed")
            if timestamp < int(row["started_at_ms"]):
                raise LedgerError("orchestration checkpoint time is invalid")
            for prior, current, label in (
                (row["goal_tokens_used"], goal_tokens_used, "goal token usage"),
                (
                    row["goal_time_used_seconds"],
                    goal_time_used_seconds,
                    "goal elapsed time",
                ),
            ):
                if prior is not None and current < int(prior):
                    raise LedgerError(f"{label} cannot decrease")
            self._connection.execute(
                "UPDATE orchestrations SET goal_tokens_used=?,"
                "goal_time_used_seconds=? WHERE project_id=? "
                "AND orchestration_id=? AND status=?",
                (
                    goal_tokens_used,
                    goal_time_used_seconds,
                    project_id,
                    str(row["orchestration_id"]),
                    str(row["status"]),
                ),
            )
            orchestration_id = str(row["orchestration_id"])
            self._append_orchestration_checkpoint_locked(
                project_id,
                orchestration_id,
                status=str(row["status"]),
                goal_tokens_used=goal_tokens_used,
                goal_time_used_seconds=goal_time_used_seconds,
                observed_at_ms=timestamp,
            )
        self.state.harden_database_files()
        return self.orchestration_report(project_id, orchestration_id)

    def bind_host_agent(
        self,
        project_id: str,
        *,
        run_handle: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Bind a live native agent identifier without storing the raw identifier."""

        if not isinstance(run_handle, str) or not re.fullmatch(
            r"hrn_[0-9a-f]{48}", run_handle
        ):
            raise LedgerError("host run handle is invalid")
        # Native agent IDs are opaque tokens: Codex IDs, Claude Code Agent tool
        # IDs (short hex) and bridge run IDs all fit this bounded form.
        if not isinstance(agent_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", agent_id
        ):
            raise LedgerError("native agent identifier is invalid")
        native_assignment_id = external_id(
            self.state.digest_key(), "asg", agent_id
        )
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT native_assignment_id,consumed_at_ms,binding_state "
                "FROM host_run_bindings "
                "WHERE project_id=? AND handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if (
                row is None
                or row["consumed_at_ms"] is not None
                or row["binding_state"] != "pending"
            ):
                raise LedgerError("host run handle is unavailable")
            if row["native_assignment_id"] not in {None, native_assignment_id}:
                raise LedgerError("host run is already bound to another native agent")
            self._connection.execute(
                "UPDATE host_run_bindings SET native_assignment_id=? "
                "WHERE project_id=? AND handle_digest=?",
                (native_assignment_id, project_id, handle_digest),
            )
        self.state.harden_database_files()
        return {"bound": True, "native_assignment_id": native_assignment_id}

    def classify_host_run_abandoned(
        self,
        project_id: str,
        *,
        run_handle: str,
        reason: str,
    ) -> dict[str, Any]:
        """Classify a pending binding only after its orchestration is paused."""

        if reason not in {
            "host_confirmed_gone", "owner_cancelled", "recovery_unavailable"
        }:
            raise LedgerError("host abandonment reason is invalid")
        if not isinstance(run_handle, str) or not re.fullmatch(
            r"hrn_[0-9a-f]{48}", run_handle
        ):
            raise LedgerError("host run handle is invalid")
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT b.binding_state,o.status FROM host_run_bindings b "
                "JOIN orchestrations o ON o.project_id=b.project_id "
                "AND o.orchestration_id=b.orchestration_id "
                "WHERE b.project_id=? AND b.handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if row is None or row["binding_state"] != "pending":
                raise LedgerError("pending host run is unavailable")
            if row["status"] != "paused":
                raise LedgerError("orchestration must be paused before abandonment")
            self._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',"
                "abandoned_reason=? WHERE project_id=? AND handle_digest=? "
                "AND binding_state='pending'",
                (reason, project_id, handle_digest),
            )
        self.state.harden_database_files()
        return {"classified": True, "binding_state": "abandoned", "reason": reason}

    def prepare_upgrade_reconciliation(
        self,
        project_id: str,
        *,
        orchestration_id: str,
        run_handle: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Describe one pending, stopped child for a detached owner signature."""

        if not isinstance(orchestration_id, str) or not re.fullmatch(
            r"orc:[0-9a-f]{64}", orchestration_id
        ):
            raise LedgerError("orchestration ID is invalid")
        if not isinstance(run_handle, str) or not re.fullmatch(
            r"hrn_[0-9a-f]{48}", run_handle
        ):
            raise LedgerError("host run handle is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            orchestration = self._connection.execute(
                "SELECT status,started_at_ms FROM orchestrations WHERE project_id=? "
                "AND orchestration_id=?", (project_id, orchestration_id)
            ).fetchone()
            binding = self._connection.execute(
                "SELECT * FROM host_run_bindings WHERE project_id=? "
                "AND orchestration_id=? AND handle_digest=?",
                (project_id, orchestration_id, handle_digest),
            ).fetchone()
            counts = self._connection.execute(
                "SELECT COUNT(*),SUM(binding_state='pending') FROM host_run_bindings "
                "WHERE project_id=? AND orchestration_id=?",
                (project_id, orchestration_id),
            ).fetchone()
            checkpoint = self._connection.execute(
                "SELECT MAX(sequence) FROM orchestration_checkpoints WHERE project_id=? "
                "AND orchestration_id=?", (project_id, orchestration_id)
            ).fetchone()
        if (
            project is None or project["status"] != "active"
            or orchestration is None
            or orchestration["status"] not in {"active", "paused"}
            or binding is None or binding["binding_state"] != "pending"
            or binding["consumed_at_ms"] is not None
            or int(counts[1] or 0) != 1
            or timestamp < int(orchestration["started_at_ms"])
        ):
            raise LedgerError("exact pending upgrade reconciliation is unavailable")
        return {
            "purpose": "codex_agents_upgrade_superseded_reconciliation_v1",
            "project_id": project_id,
            "orchestration_id": orchestration_id,
            "run_id": str(binding["run_id"]),
            "run_handle_digest": handle_digest,
            "assignment_digest": str(binding["assignment_digest"]),
            "route_digest": str(binding["route_digest"]),
            "configuration_digest": str(binding["configuration_digest"]),
            "client_version": str(binding["client_version"]),
            "binding_created_at_ms": int(binding["created_at_ms"]),
            "native_assignment_id": binding["native_assignment_id"],
            "last_progress_at_ms": int(binding["last_progress_at_ms"]),
            "boundary_status": str(orchestration["status"]),
            "opened_runs": int(counts[0]),
            "checkpoint_sequence": int(checkpoint[0] or 0),
            "owner_assertion": "native_child_stopped_and_result_superseded",
            "abandoned_reason": "upgrade_superseded",
            "terminal_status": "interrupted",
            "issued_at_ms": timestamp,
            "expires_at_ms": timestamp + UPGRADE_RECONCILIATION_TTL_MS,
            "nonce_digest": keyed_digest(self.state.digest_key(), secrets.token_bytes(32)),
        }

    def commit_upgrade_reconciliation(
        self,
        project_id: str,
        *,
        request: dict[str, Any],
        signature: bytes,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Atomically abandon one signed superseded run and interrupt its boundary."""

        expected_fields = {
            "purpose", "project_id", "orchestration_id", "run_id",
            "run_handle_digest", "assignment_digest", "route_digest",
            "configuration_digest", "client_version", "binding_created_at_ms",
            "native_assignment_id", "last_progress_at_ms",
            "boundary_status", "opened_runs", "checkpoint_sequence",
            "owner_assertion", "abandoned_reason", "terminal_status",
            "issued_at_ms", "expires_at_ms", "nonce_digest",
        }
        if not isinstance(request, dict) or set(request) != expected_fields:
            raise LedgerError("upgrade reconciliation request is invalid")
        if (
            request["purpose"] != "codex_agents_upgrade_superseded_reconciliation_v1"
            or request["project_id"] != project_id
            or request["owner_assertion"] != "native_child_stopped_and_result_superseded"
            or request["abandoned_reason"] != "upgrade_superseded"
            or request["terminal_status"] != "interrupted"
            or request["boundary_status"] not in {"active", "paused"}
            or not isinstance(request["orchestration_id"], str)
            or not re.fullmatch(r"orc:[0-9a-f]{64}", request["orchestration_id"])
            or not isinstance(request["run_id"], str)
            or not re.fullmatch(r"run:[0-9a-f]{64}", request["run_id"])
            or not all(
                isinstance(request[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", request[key])
                for key in (
                    "run_handle_digest", "assignment_digest", "route_digest",
                    "configuration_digest", "nonce_digest",
                )
            )
            or not isinstance(request["client_version"], str)
            or not 1 <= len(request["client_version"]) <= 128
            or request["native_assignment_id"] is not None
            and (
                not isinstance(request["native_assignment_id"], str)
                or not re.fullmatch(
                    r"asg:[0-9a-f]{64}", request["native_assignment_id"]
                )
            )
            or not all(
                isinstance(request[key], int) and not isinstance(request[key], bool)
                and request[key] >= 0
                for key in (
                    "binding_created_at_ms", "last_progress_at_ms",
                    "opened_runs", "checkpoint_sequence",
                    "issued_at_ms", "expires_at_ms",
                )
            )
            or request["opened_runs"] < 1
            or request["checkpoint_sequence"] < 1
            or not 0 < request["expires_at_ms"] - request["issued_at_ms"]
            <= UPGRADE_RECONCILIATION_TTL_MS
        ):
            raise LedgerError("upgrade reconciliation request is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        if not request["issued_at_ms"] <= timestamp < request["expires_at_ms"]:
            raise LedgerError("upgrade reconciliation request has expired")
        from .learning_engine import OwnerGrantAuthority

        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        if not authority.verify_upgrade_reconciliation_signature(request, signature):
            raise LedgerError("upgrade reconciliation owner signature is invalid")
        with self._lock, self._connection:
            # The ledger uses SQLite autocommit; explicitly begin before rechecks
            # so pause, classification and closure commit or roll back together.
            self._connection.execute("BEGIN IMMEDIATE")
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            orchestration = self._connection.execute(
                "SELECT * FROM orchestrations WHERE project_id=? AND orchestration_id=?",
                (project_id, request["orchestration_id"]),
            ).fetchone()
            binding = self._connection.execute(
                "SELECT * FROM host_run_bindings WHERE project_id=? AND run_id=?",
                (project_id, request["run_id"]),
            ).fetchone()
            counts = self._connection.execute(
                "SELECT COUNT(*),SUM(binding_state='pending') FROM host_run_bindings "
                "WHERE project_id=? AND orchestration_id=?",
                (project_id, request["orchestration_id"]),
            ).fetchone()
            checkpoint = self._connection.execute(
                "SELECT MAX(sequence),MAX(observed_at_ms) FROM orchestration_checkpoints "
                "WHERE project_id=? AND orchestration_id=?",
                (project_id, request["orchestration_id"]),
            ).fetchone()
            if (
                project is None or project["status"] != "active"
                or orchestration is None
                or orchestration["status"] != request["boundary_status"]
                or binding is None or binding["orchestration_id"] != request["orchestration_id"]
                or binding["binding_state"] != "pending"
                or binding["consumed_at_ms"] is not None
                or any(
                    binding[column] != request[key]
                    for column, key in (
                        ("handle_digest", "run_handle_digest"),
                        ("assignment_digest", "assignment_digest"),
                        ("route_digest", "route_digest"),
                        ("configuration_digest", "configuration_digest"),
                        ("client_version", "client_version"),
                        ("created_at_ms", "binding_created_at_ms"),
                        ("native_assignment_id", "native_assignment_id"),
                        ("last_progress_at_ms", "last_progress_at_ms"),
                    )
                )
                or int(counts[0]) != request["opened_runs"]
                or int(counts[1] or 0) != 1
                or int(checkpoint[0] or 0) != request["checkpoint_sequence"]
                or timestamp < int(checkpoint[1] or orchestration["started_at_ms"])
            ):
                raise LedgerError("exact pending upgrade reconciliation is unavailable")
            outcomes = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='outcome' AND source='host'",
                (project_id,),
            ).fetchall()
            if any(
                json.loads(bytes(row["content"])).get("run_id") == request["run_id"]
                for row in outcomes
            ):
                raise LedgerError("superseded run already has a host outcome")
            if orchestration["status"] == "active":
                self._connection.execute(
                    "UPDATE orchestrations SET status='paused' WHERE project_id=? "
                    "AND orchestration_id=? AND status='active'",
                    (project_id, request["orchestration_id"]),
                )
                self._append_orchestration_checkpoint_locked(
                    project_id, request["orchestration_id"], status="paused",
                    goal_tokens_used=orchestration["goal_tokens_used"],
                    goal_time_used_seconds=orchestration["goal_time_used_seconds"],
                    observed_at_ms=timestamp,
                )
            changed = self._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',"
                "abandoned_reason='upgrade_superseded' WHERE project_id=? AND run_id=? "
                "AND binding_state='pending' AND consumed_at_ms IS NULL",
                (project_id, request["run_id"]),
            ).rowcount
            if changed != 1:
                raise LedgerError("superseded host run was already resolved")
            self._connection.execute(
                "UPDATE orchestrations SET status='interrupted',ended_at_ms=? "
                "WHERE project_id=? AND orchestration_id=? AND status='paused'",
                (timestamp, project_id, request["orchestration_id"]),
            )
            self._append_orchestration_checkpoint_locked(
                project_id, request["orchestration_id"], status="interrupted",
                goal_tokens_used=orchestration["goal_tokens_used"],
                goal_time_used_seconds=orchestration["goal_time_used_seconds"],
                observed_at_ms=timestamp,
            )
        self.state.harden_database_files()
        return {
            "reconciled_run_id": request["run_id"],
            "request_digest": content_digest(request),
            "abandoned_reason": "upgrade_superseded",
            **self.orchestration_report(project_id, request["orchestration_id"]),
        }

    def issue_legacy_fallback_receipt(
        self,
        project_id: str,
        *,
        run_handle: str,
        task: dict[str, Any],
        primary_route: dict[str, Any],
        client_version: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Consume one abandoned primary run as trusted fallback authorization."""

        if not isinstance(run_handle, str) or not re.fullmatch(
            r"hrn_[0-9a-f]{48}", run_handle
        ):
            raise LedgerError("fallback source run handle is invalid")
        checked_task = validate_task(task)
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        task_digest = keyed_digest(
            self.state.digest_key(), canonical_json(checked_task)
        )
        route_digest = keyed_digest(
            self.state.digest_key(), canonical_json(primary_route)
        )
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT b.task_digest,b.route_digest,b.client_version,"
                "b.binding_state,b.abandoned_reason,b.native_assignment_id,"
                "b.consumed_at_ms,b.orchestration_id,o.status "
                "FROM host_run_bindings b "
                "JOIN orchestrations o ON o.project_id=b.project_id "
                "AND o.orchestration_id=b.orchestration_id "
                "WHERE b.project_id=? AND b.handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if row is None:
                raise LedgerError("fallback source run is unavailable")
            if (
                row["binding_state"] != "abandoned"
                or row["abandoned_reason"] != "recovery_unavailable"
                or row["native_assignment_id"] is not None
                or row["consumed_at_ms"] is not None
                or row["status"] != "paused"
            ):
                raise LedgerError(
                    "fallback requires one unbound recovery-unavailable run "
                    "inside a paused orchestration"
                )
            if (
                row["task_digest"] != task_digest
                or row["route_digest"] != route_digest
                or row["client_version"] != client_version
            ):
                raise LedgerError("fallback source does not match the exact primary route")
            self._connection.execute(
                "UPDATE host_run_bindings SET consumed_at_ms=? "
                "WHERE project_id=? AND handle_digest=? AND consumed_at_ms IS NULL",
                (timestamp, project_id, handle_digest),
            )
            authorization_digest = keyed_digest(
                self.state.digest_key(),
                canonical_json(
                    {
                        "task": checked_task,
                        "primary_route": primary_route,
                        "client_version": client_version,
                    }
                ),
            )
            receipt_id = f"fbr_{secrets.token_hex(24)}"
            self._connection.execute(
                "INSERT INTO repair_receipts VALUES(?,?,?,?,?,?,NULL,?)",
                (
                    project_id,
                    receipt_id,
                    authorization_digest,
                    "model_unavailable_fallback",
                    timestamp,
                    timestamp + REPAIR_RECEIPT_TTL_MS,
                    str(row["orchestration_id"]),
                ),
            )
        self.state.harden_database_files()
        return {
            "receipt_id": receipt_id,
            "authorization_digest": authorization_digest,
            "expires_at_ms": timestamp + REPAIR_RECEIPT_TTL_MS,
        }

    def verify_legacy_fallback_receipt(
        self,
        project_id: str,
        *,
        receipt_id: str,
        task: dict[str, Any],
        primary_route: dict[str, Any],
        client_version: str,
    ) -> dict[str, Any]:
        """Verify a previously issued fallback receipt against exact route state."""

        match = re.fullmatch(r"fbr_[0-9a-f]{48}", receipt_id)
        if match is None:
            raise LedgerError("legacy fallback receipt is invalid")
        checked_task = validate_task(task)
        task_digest = keyed_digest(
            self.state.digest_key(), canonical_json(checked_task)
        )
        authorization_digest = keyed_digest(
            self.state.digest_key(),
            canonical_json(
                {
                    "task": checked_task,
                    "primary_route": primary_route,
                    "client_version": client_version,
                }
            ),
        )
        timestamp = time.time_ns() // 1_000_000
        with self._lock:
            row = self._connection.execute(
                "SELECT assignment_digest,reason_code,expires_at_ms,consumed_at_ms,"
                "run_id FROM repair_receipts WHERE project_id=? AND receipt_id=?",
                (project_id, receipt_id),
            ).fetchone()
        if (
            row is None
            or row["reason_code"] != "model_unavailable_fallback"
            or row["assignment_digest"] != authorization_digest
            or int(row["expires_at_ms"]) <= timestamp
        ):
            raise LedgerError("legacy fallback receipt does not match trusted host state")
        if row["consumed_at_ms"] is not None:
            with self._lock:
                bound = self._connection.execute(
                    "SELECT task_digest,client_version,binding_state "
                    "FROM host_run_bindings WHERE project_id=? AND run_id=?",
                    (project_id, row["run_id"]),
                ).fetchone()
            if (
                bound is None
                or bound["task_digest"] != task_digest
                or bound["client_version"] != client_version
                or bound["binding_state"] not in {"pending", "terminal"}
            ):
                raise LedgerError(
                    "legacy fallback receipt is not bound to its fallback host run"
                )
        return {
            "receipt_id": receipt_id,
            "authorization_digest": authorization_digest,
            "consumed": row["consumed_at_ms"] is not None,
            "verified": True,
        }

    def orchestration_report(
        self, project_id: str, orchestration_id: str
    ) -> dict[str, Any]:
        """Return content-free, non-duplicating statistics for one boundary."""

        with self._lock:
            orchestration = self._connection.execute(
                "SELECT * FROM orchestrations WHERE project_id=? AND orchestration_id=?",
                (project_id, orchestration_id),
            ).fetchone()
            if orchestration is None:
                raise LedgerError("orchestration is unavailable")
            bindings = self._connection.execute(
                "SELECT run_id,agent_type,created_at_ms,consumed_at_ms,"
                "native_assignment_id,binding_state,deadline_observed,"
                "observation_deadline_at_ms FROM host_run_bindings "
                "WHERE project_id=? AND orchestration_id=? ORDER BY created_at_ms",
                (project_id, orchestration_id),
            ).fetchall()
            outcome_rows = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_type='outcome' AND source='host'",
                (project_id,),
            ).fetchall()
            qa_rows = self._connection.execute(
                "SELECT host_outcome_digest,accepted FROM operational_qa "
                "WHERE project_id=?",
                (project_id,),
            ).fetchall()
            checkpoints = self._connection.execute(
                "SELECT sequence,status,goal_tokens_used,goal_time_used_seconds,"
                "observed_at_ms FROM orchestration_checkpoints "
                "WHERE project_id=? AND orchestration_id=? ORDER BY sequence",
                (project_id, orchestration_id),
            ).fetchall()
        outcomes = {
            item["run_id"]: (item, str(row["content_digest"]))
            for row in outcome_rows
            for item in [json.loads(bytes(row["content"]))]
        }
        qa_by_outcome = {
            str(row["host_outcome_digest"]): bool(row["accepted"])
            for row in qa_rows
        }
        usage_fields = tuple(empty_usage())
        usage_totals: dict[str, int | None] = {field: 0 for field in usage_fields}
        usage_unknown_runs = {field: 0 for field in usage_fields}
        by_model: dict[str, int] = {}
        by_effort: dict[str, int] = {}
        by_role: dict[str, int] = {}
        by_agent_type: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        repairs = 0
        no_progress = 0
        astra_runs = 0
        qa_passed = 0
        qa_failed = 0
        qa_pending = 0
        durations: list[int] = []
        for binding in bindings:
            run_id = str(binding["run_id"])
            outcome_entry = outcomes.get(run_id)
            agent_type = str(binding["agent_type"] or "unknown")
            by_agent_type[agent_type] = by_agent_type.get(agent_type, 0) + 1
            terminal = binding["consumed_at_ms"]
            if terminal is not None:
                durations.append(int(terminal) - int(binding["created_at_ms"]))
            if outcome_entry is None:
                lifecycle_status = {
                    "abandoned": "abandoned",
                    "terminal": "terminal_content_unavailable",
                    "pending": "pending",
                }[str(binding["binding_state"])]
                status_counts[lifecycle_status] = (
                    status_counts.get(lifecycle_status, 0) + 1
                )
                if lifecycle_status == "pending":
                    qa_pending += 1
                for field in usage_fields:
                    usage_unknown_runs[field] += 1
                continue
            outcome, outcome_digest = outcome_entry
            if outcome["task_slice"].startswith("verification-"):
                pass
            elif outcome_digest not in qa_by_outcome:
                qa_pending += 1
            elif qa_by_outcome[outcome_digest]:
                qa_passed += 1
            else:
                qa_failed += 1
            for field, target in (
                ("model", by_model),
                ("effort", by_effort),
                ("role", by_role),
            ):
                value = str(outcome[field])
                target[value] = target.get(value, 0) + 1
            status = str(outcome["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            repairs += int(outcome["repairs"])
            no_progress += int(outcome["no_progress_events"])
            astra_runs += int(outcome["model"] == "gpt-6-astra")
            for field in usage_fields:
                value = outcome["usage"][field]
                if value is None:
                    usage_unknown_runs[field] += 1
                    usage_totals[field] = None
                elif usage_totals[field] is not None:
                    usage_totals[field] = int(usage_totals[field]) + int(value)
        ended = orchestration["ended_at_ms"]
        return {
            "orchestration_id": orchestration_id,
            "status": str(orchestration["status"]),
            "started_at_ms": int(orchestration["started_at_ms"]),
            "ended_at_ms": int(ended) if ended is not None else None,
            "duration_ms": (
                int(ended) - int(orchestration["started_at_ms"])
                if ended is not None else None
            ),
            "runs": {
                "opened": len(bindings),
                "terminal": sum(
                    row["binding_state"] == "terminal" for row in bindings
                ),
                "pending": sum(
                    row["binding_state"] == "pending" for row in bindings
                ),
                "abandoned": sum(
                    row["binding_state"] == "abandoned" for row in bindings
                ),
                "current_active": (
                    sum(row["binding_state"] == "pending" for row in bindings)
                    if orchestration["status"] == "active"
                    else 0
                ),
                "native_agent_bound": sum(
                    row["native_assignment_id"] is not None for row in bindings
                ),
                "deadlines_observed": sum(
                    bool(row["deadline_observed"]) for row in bindings
                ),
            },
            "checkpoints": [
                {
                    "sequence": int(row["sequence"]),
                    "status": str(row["status"]),
                    "goal_tokens_used": row["goal_tokens_used"],
                    "goal_time_used_seconds": row["goal_time_used_seconds"],
                    "observed_at_ms": int(row["observed_at_ms"]),
                }
                for row in checkpoints
            ],
            "by_model": dict(sorted(by_model.items())),
            "by_effort": dict(sorted(by_effort.items())),
            "by_role": dict(sorted(by_role.items())),
            "by_agent_type": dict(sorted(by_agent_type.items())),
            "outcome_status": dict(sorted(status_counts.items())),
            "repairs": repairs,
            "no_progress_events": no_progress,
            "astra_runs": astra_runs,
            "qa": {
                "passed": qa_passed,
                "failed": qa_failed,
                "pending": qa_pending,
                "attention_required": qa_failed > 0 or qa_pending > 0,
            },
            "run_duration_ms": {
                "total": sum(durations),
                "minimum": min(durations) if durations else None,
                "maximum": max(durations) if durations else None,
            },
            "usage": {
                "host_run_totals": usage_totals,
                "unknown_runs": usage_unknown_runs,
                "goal_total_tokens": orchestration["goal_tokens_used"],
                "goal_time_used_seconds": orchestration["goal_time_used_seconds"],
                "goal_usage_source": (
                    "host_reported"
                    if orchestration["goal_tokens_used"] is not None
                    or orchestration["goal_time_used_seconds"] is not None
                    else "unavailable"
                ),
                "child_attribution": (
                    "complete"
                    if bindings and all(count == 0 for count in usage_unknown_runs.values())
                    else "unknown"
                ),
            },
        }

    def open_host_run(
        self,
        project_id: str,
        *,
        assignment: dict[str, Any],
        task: dict[str, Any],
        route: dict[str, Any],
        execution_configuration: dict[str, Any],
        client_version: str,
        orchestration_handle: str,
        fallback_receipt: str | None = None,
        host_profile: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist a trusted assigned-run start and return a single-use opaque handle.

        Admission is always evaluated here, from ledger-held evidence and this
        process's environment; a denial is stored and raised before any binding.
        """

        checked_assignment = validate_assignment_shape(assignment)
        checked_task = validate_task(task)
        checked_configuration = validate_execution_configuration(
            execution_configuration
        )
        capability = self._client_capabilities.get(client_version)
        if capability is None or "host" not in capability.get("producers", []):
            raise LedgerError("host lifecycle client is not authorized")
        if any(
            checked_assignment[field] != checked_configuration[field]
            for field in ("model", "effort", "role")
        ):
            raise LedgerError("host assignment does not match its route configuration")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        observation_seconds = route.get(
            "observation_deadline_seconds",
            checked_assignment["bounds"]["timeout_seconds"],
        )
        if (
            not isinstance(observation_seconds, int)
            or isinstance(observation_seconds, bool)
            or observation_seconds < 1
        ):
            raise LedgerError("route observation deadline is invalid")
        observation_deadline_at_ms = timestamp + observation_seconds * 1000
        from .providers import AdmissionDenied

        provider = route.get("provider", "codex")
        # Admission runs on the ledger's clock, so a caller-chosen time cannot
        # make evidence look reset or stale.
        admitted_at = self._clock_ms()
        admission = self.evaluate_admission(
            project_id, provider=provider, environ=os.environ, now_ms=admitted_at
        )
        run_handle = f"hrn_{secrets.token_hex(24)}"
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        run_id = external_id(self.state.digest_key(), "run", run_handle)
        assignment_digest = keyed_digest(
            self.state.digest_key(), canonical_json(checked_assignment)
        )
        task_digest = keyed_digest(
            self.state.digest_key(), canonical_json(checked_task)
        )
        route_digest = keyed_digest(self.state.digest_key(), canonical_json(route))
        configuration_digest = content_digest(checked_configuration)
        task_slice = routing_task_slice(checked_task)
        event = validate_event(
            {
                "schema_version": 1,
                "record_type": "event",
                "record_id": external_id(
                    self.state.digest_key(),
                    "evt",
                    f"{run_id}\x00{checked_assignment['assignment_id']}\x00started",
                ),
                "run_id": run_id,
                "turn_id": None,
                "assignment_id": checked_assignment["assignment_id"],
                "parent_run_id": None,
                "observed_at_ms": timestamp,
                "event_code": "subagent_started",
                "model": checked_assignment["model"],
                "effort": checked_assignment["effort"],
                "role": checked_assignment["role"],
                "task_family": checked_task["family"],
                "task_slice": task_slice,
                "topology": checked_configuration["topology"],
                "configuration_digest": configuration_digest,
                "status": "started",
                "stop_reason": None,
                "tool_digest": None,
                "item_digest": None,
                "coverage": "lifecycle_partial",
                "usage": empty_usage(),
            }
        )
        try:
            with self._lock, self._connection:
                project = self._connection.execute(
                    "SELECT status FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if not project or project["status"] != "active":
                    raise LedgerError("project binding is not active")
                orchestration = self._orchestration_for_handle_locked(
                    project_id, orchestration_handle
                )
                if orchestration["status"] != "active":
                    raise LedgerError("orchestration is not active")
                orchestration_id = str(orchestration["orchestration_id"])
                fallback = route.get("fallback")
                if (fallback_receipt is None) != (fallback is None):
                    raise LedgerError("fallback route and receipt must appear together")
                if fallback_receipt is not None:
                    if (
                        not isinstance(fallback, dict)
                        or fallback.get("receipt_id") != fallback_receipt
                        or not isinstance(fallback.get("authorization_digest"), str)
                    ):
                        raise LedgerError("fallback route receipt binding is invalid")
                    receipt = self._connection.execute(
                        "SELECT assignment_digest,reason_code,expires_at_ms,"
                        "consumed_at_ms,run_id FROM repair_receipts "
                        "WHERE project_id=? AND receipt_id=?",
                        (project_id, fallback_receipt),
                    ).fetchone()
                    if (
                        receipt is None
                        or receipt["reason_code"] != "model_unavailable_fallback"
                        or receipt["assignment_digest"]
                        != fallback["authorization_digest"]
                        or int(receipt["expires_at_ms"]) <= timestamp
                        or receipt["consumed_at_ms"] is not None
                        or receipt["run_id"] != orchestration_id
                    ):
                        raise LedgerError("fallback receipt is unavailable for this run")
                pending = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM host_run_bindings WHERE project_id=? "
                        "AND binding_state='pending'",
                        (project_id,),
                    ).fetchone()[0]
                )
                if pending >= MAX_PENDING_HOST_RUNS:
                    raise LedgerError("pending host run capacity is exhausted")
                # Re-check under the write lock before anything is written, so two
                # opens cannot both use the same Claude bootstrap, take the last
                # place under the run cap, or race past newer evidence. The
                # clock is read again here, so an opener that waited for the
                # lock is judged, and its decision stamped, at the time it holds it.
                admitted_at = self._clock_ms()
                admission = self._evaluate_admission_locked(
                    project_id, provider=provider, environ=os.environ, timestamp=admitted_at
                )
                if admission["decision"] != "pass":
                    raise _RecheckDenied(admission)
                self._connection.execute(
                    "INSERT INTO host_run_bindings("
                    "project_id,handle_digest,run_id,assignment_digest,task_digest,"
                    "task_id_digest,"
                    "route_digest,configuration_digest,client_version,created_at_ms,"
                    "expires_at_ms,consumed_at_ms,orchestration_id,native_assignment_id,"
                    "agent_type,usage_complete,binding_state,abandoned_reason,"
                    "observation_started_at_ms,observation_deadline_at_ms,"
                    "last_progress_at_ms,deadline_observed) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,NULL,?,NULL,'pending',NULL,"
                    "?,?,?,0)",
                    (
                        project_id, handle_digest, run_id, assignment_digest,
                        task_digest,
                        keyed_digest(self.state.digest_key(), checked_task["id"]),
                        route_digest, configuration_digest, client_version,
                        timestamp, timestamp + HOST_RUN_TTL_MS, orchestration_id,
                        route.get("agent_type", checked_assignment["role"]),
                        timestamp, observation_deadline_at_ms, timestamp,
                    ),
                )
                if fallback_receipt is not None:
                    changed = self._connection.execute(
                        "UPDATE repair_receipts SET consumed_at_ms=?,run_id=? "
                        "WHERE project_id=? AND receipt_id=? "
                        "AND consumed_at_ms IS NULL AND expires_at_ms>?",
                        (
                            timestamp,
                            run_id,
                            project_id,
                            fallback_receipt,
                            timestamp,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise LedgerError("fallback receipt was already consumed")
                self._connection.execute(
                    "INSERT OR IGNORE INTO sessions VALUES(?,?,?,?)",
                    (project_id, handle_digest, "host", timestamp),
                )
                self._connection.execute(
                    "INSERT INTO run_provenance VALUES(?,?,?,?,?,?)",
                    (
                        project_id, run_id, "host", client_version,
                        handle_digest, timestamp,
                    ),
                )
                self._append_checked_locked(
                    project_id, event, producer="host", timestamp=timestamp
                )
                self._store_admission_locked(
                    project_id, admission, run_id=run_id, timestamp=admitted_at
                )
                if host_profile is not None:
                    if host_profile.get("host_client") != orchestration["client_version"]:
                        raise LedgerError("host run profile differs from the orchestration host")
                    material = {
                        "schema_version": 1,
                        "record_type": "host_run_profile",
                        "run_id": run_id,
                        **host_profile,
                        "observed_at_ms": admitted_at,
                    }
                    try:
                        profile = validate_learning_record(
                            {**material, "record_id": f"host_run_profile:{content_digest(material)}"}
                        )
                    except ContractError as exc:
                        raise LedgerError("host run profile is invalid") from exc
                    self._append_checked_locked(
                        project_id, profile, producer="lifecycle", timestamp=admitted_at
                    )
        except _RecheckDenied as denied:
            # The denial is stored like any other, after the open is abandoned.
            with self._lock, self._connection:
                self._store_admission_locked(
                    project_id, denied.decision, run_id=None,
                    timestamp=denied.decision["observed_at_ms"],
                )
            self.state.harden_database_files()
            raise AdmissionDenied(denied.decision["reason_code"]) from None
        self.state.harden_database_files()
        return {
            "run_handle": run_handle,
            "admission": admission["reason_code"],
            "status": "started",
            "coverage": "lifecycle_partial",
            "usage": empty_usage(),
            "observation_started_at_ms": timestamp,
            "observation_deadline_at_ms": observation_deadline_at_ms,
        }

    def _clock_ms(self) -> int:
        """The ledger's own time for evidence it stamps; callers never supply it."""

        return time.time_ns() // 1_000_000

    def _store_provider_observations(
        self,
        project_id: str,
        records: list[dict[str, Any]],
        *,
        timestamp: int,
    ) -> list[dict[str, Any]]:
        try:
            checked = [validate_learning_record(record) for record in records]
        except ContractError as exc:
            raise LedgerError("record admission rejected") from exc
        if any(record["record_type"] not in PROVIDER_OBSERVATION_TYPES for record in checked):
            raise LedgerError("provider observation type is unsupported")
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            results = [
                self._append_checked_locked(
                    project_id, record, producer="provider", timestamp=timestamp
                )
                for record in checked
            ]
        self.state.harden_database_files()
        return results

    def observe_provider(
        self,
        project_id: str,
        provider: str,
        *,
        project_root: Path,
        environ: Any = None,
        run_handle: str | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run one non-spending observation and store it under a keyed fingerprint.

        The ledger runs the provider client itself and derives the fingerprint
        with its own key, so no caller can supply observation records. With a
        run handle, the headroom and billing it reads are bound to that open
        run and to a phase, before or after its launch.
        """

        from .providers import observe_claude, observe_codex

        run_id = None
        if (run_handle is None) != (phase is None) or phase not in {None, "before", "after"}:
            raise LedgerError("a run observation needs its run and its phase")
        if run_handle is not None:
            with self._lock:
                run_id = self._open_run_for_provider_locked(project_id, run_handle, provider)
        timestamp = self._clock_ms()
        if provider == "claude":
            records = observe_claude(
                digest_key=self.state.digest_key(),
                observed_at_ms=timestamp,
                environ=environ,
            )
        elif provider == "codex":
            records = observe_codex(
                project_root,
                digest_key=self.state.digest_key(),
                observed_at_ms=timestamp,
                environ=environ,
                run_id=run_id,
                phase=phase,
            )
        else:
            raise LedgerError("provider is unsupported")
        self._store_provider_observations(project_id, records, timestamp=timestamp)
        return records

    def _open_run_for_provider_locked(
        self, project_id: str, run_handle: str, provider: str
    ) -> str:
        """The run ID of an open run whose stored admission passed for `provider`."""

        if not isinstance(run_handle, str):
            raise LedgerError("host run handle is invalid")
        binding = self._connection.execute(
            "SELECT run_id,binding_state,consumed_at_ms FROM host_run_bindings "
            "WHERE project_id=? AND handle_digest=?",
            (project_id, keyed_digest(self.state.digest_key(), run_handle)),
        ).fetchone()
        if binding is None:
            raise LedgerError("host run is unavailable")
        if binding["binding_state"] != "pending" or binding["consumed_at_ms"] is not None:
            raise LedgerError("host run is no longer open for observations")
        run_id = str(binding["run_id"])
        if self._run_admission_locked(project_id, run_id)["provider"] != provider:
            raise LedgerError("observation provider differs from the run's admission")
        return run_id

    def record_observation_unavailable(
        self,
        project_id: str,
        *,
        run_handle: str,
        provider: str,
        phase: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Persist that a run's provider reading could not be taken, and why.

        The record is never evidence for admission; it keeps a failed reading
        visible in the ledger instead of dropping it.
        """

        timestamp = self._clock_ms()
        with self._lock, self._connection:
            run_id = self._open_run_for_provider_locked(project_id, run_handle, provider)
            material = {
                "schema_version": 1,
                "record_type": "observation_unavailable",
                "provider": provider,
                "run_id": run_id,
                "phase": phase,
                "reason_code": reason_code,
                "observed_at_ms": timestamp,
            }
            try:
                record = validate_learning_record({
                    **material,
                    "record_id": f"observation_unavailable:{content_digest(material)}",
                })
            except ContractError as exc:
                raise LedgerError("unavailable observation is invalid") from exc
            self._append_checked_locked(
                project_id, record, producer="provider", timestamp=timestamp
            )
        self.state.harden_database_files()
        return record

    def record_provider_usage(
        self,
        project_id: str,
        *,
        run_handle: str,
        provider: str,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a dispatched run's client-observed usage, once, linked to the run."""

        timestamp = self._clock_ms()
        with self._lock, self._connection:
            run_id = self._open_run_for_provider_locked(project_id, run_handle, provider)
            if self._connection.execute(
                "SELECT 1 FROM records WHERE project_id=? AND record_type='dispatch_intent' "
                "AND json_extract(CAST(content AS TEXT),'$.run_id')=?",
                (project_id, run_id),
            ).fetchone() is None:
                raise LedgerError("only a dispatched run records its usage")
            if self._connection.execute(
                "SELECT 1 FROM records WHERE project_id=? AND record_type='provider_usage' "
                "AND json_extract(CAST(content AS TEXT),'$.run_id')=?",
                (project_id, run_id),
            ).fetchone() is not None:
                raise LedgerError("host run already recorded its usage")
            if not isinstance(usage, dict):
                raise LedgerError("provider usage is invalid")
            material = {
                **usage,
                "schema_version": 1,
                "record_type": "provider_usage",
                "provider": provider,
                "run_id": run_id,
                "phase": "after",
                "observed_at_ms": timestamp,
            }
            material.pop("record_id", None)
            try:
                record = validate_learning_record({
                    **material, "record_id": f"provider_usage:{content_digest(material)}",
                })
            except ContractError as exc:
                raise LedgerError("provider usage is invalid") from exc
            self._append_checked_locked(
                project_id, record, producer="provider", timestamp=timestamp
            )
        self.state.harden_database_files()
        return record

    def admitted_client(self, project_id: str, run_handle: str) -> dict[str, Any]:
        """The identity a run was admitted on: its account and the client it saw.

        Read from the stored admission's own evidence, so the launch re-check
        compares against what was admitted, never against current facts.
        """

        with self._lock:
            binding = self._connection.execute(
                "SELECT run_id FROM host_run_bindings WHERE project_id=? AND handle_digest=?",
                (project_id, keyed_digest(self.state.digest_key(), run_handle)),
            ).fetchone()
            if binding is None:
                raise LedgerError("host run is unavailable")
            run_id = str(binding["run_id"])
            admission = self._run_admission_locked(project_id, run_id)
            identities = []
            for digest in admission["evidence_digests"]:
                row = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? AND content_digest=? "
                    "AND record_type='provider_identity' AND source='provider'",
                    (project_id, digest),
                ).fetchone()
                if row is not None:
                    identities.append(json.loads(bytes(row["content"])))
        if len(identities) != 1:
            raise LedgerError("host run admission has no single admitted identity")
        try:
            identity = validate_learning_record(identities[0])
        except ContractError as exc:
            raise LedgerError("admitted identity is invalid") from exc
        if (
            identity["provider"] != admission["provider"]
            or identity["account_fingerprint"] != admission["account_fingerprint"]
        ):
            raise LedgerError("admitted identity differs from the admission")
        return {
            "provider": identity["provider"],
            "account_fingerprint": identity["account_fingerprint"],
            "client_version": identity["client_version"],
        }

    def observe_claude_billing(
        self,
        project_id: str,
        *,
        project_root: Path,
        environ: Any = None,
    ) -> dict[str, Any]:
        """Observe the Claude identity, then probe billing and headroom once.

        Native Claude children emit no rate-limit event, so this probe is the
        billing evidence for a Claude primary. It is a real, small request, so
        it passes the same admission rules as a run first, shares the single
        bootstrap allowance, and reuses evidence younger than five minutes.
        Only its rate-limit event is kept.
        """

        from .bridge import claude_billing_probe
        from .claude_host import ClaudeHostDenied, check_backend
        from .providers import AdmissionDenied, claude_rate_limit_records

        environ = os.environ if environ is None else environ

        def deny(reason: str, timestamp: int, decision: dict[str, Any] | None = None):
            record = decision or {
                "provider": "claude", "decision": "deny", "reason_code": reason,
                "admission_basis": None, "account_fingerprint": None,
                "evidence_digests": [], "observed_at_ms": timestamp,
            }
            with self._lock, self._connection:
                self._store_probe_admission_locked(project_id, record, timestamp=timestamp)
            self.state.harden_database_files()

        try:
            check_backend(environ, project_root)
        except ClaudeHostDenied as exc:
            deny(exc.reason_code, self._clock_ms())
            raise
        identity = self.observe_provider(
            project_id, "claude", project_root=project_root, environ=environ
        )[0]
        fingerprint = identity["account_fingerprint"]
        with self._lock, self._connection:
            # Read under the lock that orders admission decisions.
            timestamp = self._clock_ms()
            billing = [
                record for record, _ in self._provider_records_locked(
                    project_id, "provider_billing", "claude"
                )
                if record["account_fingerprint"] == fingerprint
            ]
            latest = max(billing, key=lambda record: record["observed_at_ms"], default=None)
            if latest is not None and 0 <= timestamp - latest["observed_at_ms"] < (
                PROBE_REUSE_SECONDS * 1000
            ):
                # Fresh evidence bounds the spend when children open back to back.
                headroom = [
                    record for record, _ in self._provider_records_locked(
                        project_id, "provider_headroom", "claude"
                    )
                    if record["account_fingerprint"] == fingerprint
                ]
                return {
                    "probe": "reused",
                    "records": [identity, *headroom[-1:], latest],
                }
            decision = self._evaluate_admission_locked(
                project_id, provider="claude", environ=environ, timestamp=timestamp
            )
            if decision["decision"] == "pass":
                # Stored before launch, in the same transaction as the decision,
                # so a crash or a repeat cannot take the bootstrap twice.
                self._store_probe_admission_locked(
                    project_id, decision, timestamp=timestamp
                )
        if decision["decision"] != "pass":
            deny(decision["reason_code"], timestamp, decision)
            raise AdmissionDenied(decision["reason_code"])
        self.state.harden_database_files()
        info = claude_billing_probe(environ)
        observed = self._clock_ms()
        records = claude_rate_limit_records(
            info, account_fingerprint=fingerprint, observed_at_ms=observed
        )
        self._store_provider_observations(project_id, records, timestamp=observed)
        return {"probe": "launched", "records": [identity, *records]}

    def _store_probe_admission_locked(
        self, project_id: str, decision: dict[str, Any], *, timestamp: int
    ) -> None:
        material = {"schema_version": 1, "record_type": "probe_admission", **decision}
        try:
            record = validate_learning_record(
                {**material, "record_id": f"probe_admission:{content_digest(material)}"}
            )
        except ContractError as exc:
            raise LedgerError("probe admission decision is invalid") from exc
        self._append_checked_locked(
            project_id, record, producer="lifecycle", timestamp=timestamp
        )

    def record_claude_rate_limit_event(
        self,
        project_id: str,
        *,
        run_handle: str,
        rate_limit_info: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Record a rate-limit event of an open, admitted Claude run.

        The fingerprint comes from the run's stored admission and the time from
        the ledger's clock, and events are accepted only before the run's result
        is committed. Every event that is more restrictive than what the run
        already recorded is kept, so a rejection late in a run blocks the next
        admission. A less restrictive event is dropped, so a caller cannot use a
        later favourable event to replace the run's evidence. Returns the
        records kept.
        """

        from .providers import claude_rate_limit_records

        timestamp = self._clock_ms()
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock, self._connection:
            binding = self._connection.execute(
                "SELECT run_id,binding_state,consumed_at_ms FROM host_run_bindings "
                "WHERE project_id=? AND handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if binding is None:
                raise LedgerError("host run is unavailable")
            if binding["binding_state"] != "pending" or binding["consumed_at_ms"] is not None:
                raise LedgerError("host run is no longer open for observations")
            run_id = str(binding["run_id"])
            decision = self._run_admission_locked(project_id, run_id)
            if decision["provider"] != "claude":
                raise ContractError("rate-limit event does not belong to a Claude run")
            prior = [
                json.loads(bytes(row["content"]))
                for row in self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? AND source='provider' "
                    "AND record_type IN ('provider_headroom','provider_billing') "
                    "AND json_extract(CAST(content AS TEXT),'$.run_id')=? "
                    "AND json_extract(CAST(content AS TEXT),'$.phase')='during' "
                    "ORDER BY source_sequence",
                    (project_id, run_id),
                ).fetchall()
            ]
            if len(prior) >= MAX_RUN_RATE_LIMIT_RECORDS:
                # The run is fenced: its restriction was merged when it reached
                # the cap, and the bridge stopped it.
                raise ContractError("host run recorded too many rate-limit events")
            records = claude_rate_limit_records(
                rate_limit_info,
                account_fingerprint=decision["account_fingerprint"],
                observed_at_ms=timestamp,
                run_id=run_id,
                phase="during",
            )
            try:
                checked = [validate_learning_record(record) for record in records]
            except ContractError as exc:
                raise LedgerError("record admission rejected") from exc
            kept = [
                record for record in checked
                if _more_restrictive(record, [
                    item for item in prior if item["record_type"] == record["record_type"]
                ])
            ]
            overflow = bool(kept) and len(prior) + len(kept) >= MAX_RUN_RATE_LIMIT_RECORDS
            if overflow:
                # At the cap the run's whole restriction is merged into one
                # record, the highest status with the latest reset, and the run
                # is fenced: the bridge stops it and later events are refused.
                headroom = [item for item in prior if item["record_type"] == "provider_headroom"]
                new = [item for item in checked if item["record_type"] == "provider_headroom"]
                try:
                    merged = validate_learning_record(
                        _merged_restriction([*headroom, *new], observed_at_ms=timestamp)
                    )
                except ContractError as exc:
                    raise LedgerError("record admission rejected") from exc
                kept = [
                    merged,
                    *(item for item in kept if item["record_type"] == "provider_billing"),
                ]
            for record in kept:
                self._append_checked_locked(
                    project_id, record, producer="provider", timestamp=timestamp
                )
        self.state.harden_database_files()
        if overflow:
            raise ContractError("host run recorded too many rate-limit events")
        return kept

    def _provider_records_locked(
        self, project_id: str, record_type: str, provider: str
    ) -> list[tuple[dict[str, Any], str]]:
        rows = self._connection.execute(
            "SELECT content,content_digest,source FROM records "
            "WHERE project_id=? AND record_type=? "
            "AND json_extract(CAST(content AS TEXT),'$.provider')=? "
            "ORDER BY source_sequence",
            (project_id, record_type, provider),
        ).fetchall()
        result = []
        for row in rows:
            record = json.loads(bytes(row["content"]))
            # Attestations count only when an owner signature was verified.
            if record_type == "billing_attestation" and (
                row["source"] != "owner"
                or self._connection.execute(
                    "SELECT 1 FROM owner_attestations "
                    "WHERE project_id=? AND subject_record_id=?",
                    (project_id, record["record_id"]),
                ).fetchone() is None
            ):
                continue
            result.append((record, str(row["content_digest"])))
        return result

    def _admission_settings(self, provider: str) -> dict[str, Any] | None:
        """The provider's committed admission settings; None if they cannot be read."""

        from .routing import admission_settings, load_policy

        try:
            policy = (
                self._admission_policy
                if self._admission_policy is not None
                else load_policy(INSTALLED_ROUTING_POLICY)
            )
            return admission_settings(policy, provider)
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            return None

    def client_version_reviewed(self, provider: str, version: Any) -> bool:
        """Whether a client version is inside the provider's committed reviewed range."""

        from .learning_contracts import parse_semver

        settings = self._admission_settings(provider)
        parsed = parse_semver(version)
        if settings is None or parsed is None:
            return False
        versions = settings["client_versions"]
        minimum = parse_semver(versions["minimum"])
        maximum = parse_semver(versions["maximum_exclusive"])
        return minimum is not None and maximum is not None and minimum <= parsed < maximum

    def _prior_run_admissions_locked(
        self, provider: str, timestamp: int
    ) -> list[tuple[str, int]]:
        """Passing run admissions for a provider since one window before `timestamp`.

        The cap belongs to the account, so admissions in every project of this
        ledger count; fingerprints use the ledger-wide key and compare across
        projects. Read under the writer lock with the decision that uses them,
        so two opens cannot both take the last place under the cap. Probe
        admissions are a separate record type and never count.
        """

        from .providers import FIVE_HOURS_MS

        rows = self._connection.execute(
            "SELECT json_extract(CAST(content AS TEXT),'$.account_fingerprint'),"
            "observed_at_ms FROM records WHERE "
            "record_type='admission_decision' AND source='lifecycle' "
            "AND json_extract(CAST(content AS TEXT),'$.provider')=? "
            "AND json_extract(CAST(content AS TEXT),'$.decision')='pass' "
            "AND json_extract(CAST(content AS TEXT),'$.run_id') IS NOT NULL "
            "AND observed_at_ms>=?",
            (provider, timestamp - FIVE_HOURS_MS),
        ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows if row[0] is not None]

    def _window_headroom_locked(
        self, provider: str, timestamp: int
    ) -> list[tuple[dict[str, Any], str]]:
        """Headroom readings from every project that can place the cap window.

        The cap belongs to the account, so its window is placed from every
        project's readings, like the count. A five-hour reset still ahead was
        observed less than five hours ago, so older readings cannot matter.
        """

        from .providers import FIVE_HOURS_MS

        rows = self._connection.execute(
            "SELECT content,content_digest FROM records WHERE "
            "record_type='provider_headroom' AND source='provider' "
            "AND json_extract(CAST(content AS TEXT),'$.provider')=? "
            "AND observed_at_ms>=? ORDER BY observed_at_ms",
            (provider, timestamp - FIVE_HOURS_MS),
        ).fetchall()
        return [(json.loads(bytes(row["content"])), str(row["content_digest"])) for row in rows]

    def _evaluate_admission_locked(
        self, project_id: str, *, provider: str, environ: Any, timestamp: int
    ) -> dict[str, Any]:
        from .providers import evaluate_admission

        project = self._connection.execute(
            "SELECT status FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if not project or project["status"] != "active":
            raise LedgerError("project binding is not active")
        return evaluate_admission(
            admission_policy=self._admission_settings(provider),
            prior_run_admissions=self._prior_run_admissions_locked(provider, timestamp),
            provider=provider,
            identities=self._provider_records_locked(
                project_id, "provider_identity", provider
            ),
            attestations=self._provider_records_locked(
                project_id, "billing_attestation", provider
            ),
            billing=self._provider_records_locked(
                project_id, "provider_billing", provider
            ),
            headroom=self._provider_records_locked(
                project_id, "provider_headroom", provider
            ),
            window_headroom=self._window_headroom_locked(provider, timestamp),
            prior_claude_admissions={
                str(row[0])
                for row in self._connection.execute(
                    "SELECT json_extract(CAST(content AS TEXT),'$.account_fingerprint') "
                    "FROM records WHERE project_id=? "
                    "AND record_type IN ('admission_decision','probe_admission') "
                    "AND json_extract(CAST(content AS TEXT),'$.provider')='claude' "
                    "AND json_extract(CAST(content AS TEXT),'$.decision')='pass'",
                    (project_id,),
                ).fetchall()
                if row[0] is not None
            },
            environ=environ,
            now_ms=timestamp,
        )

    def evaluate_admission(
        self,
        project_id: str,
        *,
        provider: str,
        environ: Any,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate admission from stored evidence; a denial is stored and raised.

        The returned decision is informational: only the ledger binds a decision
        to a run, inside open_host_run or a session binding.
        """

        from .providers import AdmissionDenied

        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            decision = self._evaluate_admission_locked(
                project_id, provider=provider, environ=environ, timestamp=timestamp
            )
            if decision["decision"] == "deny":
                self._store_admission_locked(
                    project_id, decision, run_id=None, timestamp=timestamp
                )
        self.state.harden_database_files()
        if decision["decision"] == "deny":
            raise AdmissionDenied(decision["reason_code"])
        return decision

    def _store_admission_locked(
        self,
        project_id: str,
        decision: dict[str, Any],
        *,
        run_id: str | None,
        timestamp: int,
    ) -> dict[str, Any]:
        from .providers import admission_record

        try:
            record = admission_record(decision, run_id)
        except ContractError as exc:
            raise LedgerError("admission decision is invalid") from exc
        for digest in record["evidence_digests"]:
            if self._connection.execute(
                "SELECT 1 FROM records WHERE project_id=? AND content_digest=?",
                (project_id, digest),
            ).fetchone() is None:
                raise LedgerError("admission evidence is unavailable")
        self._append_checked_locked(
            project_id, record, producer="lifecycle", timestamp=timestamp
        )
        return record

    def _run_admission_locked(self, project_id: str, run_id: str) -> dict[str, Any]:
        rows = self._connection.execute(
            "SELECT content,source FROM records WHERE project_id=? "
            "AND record_type='admission_decision' "
            "AND json_extract(CAST(content AS TEXT),'$.run_id')=?",
            (project_id, run_id),
        ).fetchall()
        if len(rows) != 1 or rows[0]["source"] != "lifecycle":
            raise LedgerError("host run has no stored admission decision")
        try:
            decision = validate_learning_record(json.loads(bytes(rows[0]["content"])))
        except (ContractError, TypeError, ValueError) as exc:
            raise LedgerError("host run admission is invalid") from exc
        if decision["record_type"] != "admission_decision" or decision["decision"] != "pass":
            raise LedgerError("host run admission is not a pass")
        return decision

    def record_dispatch_intent(
        self,
        project_id: str,
        *,
        run_handle: str,
        assignment: dict[str, Any],
        task: dict[str, Any],
        route: dict[str, Any],
        deadline_seconds: int,
        claim: bool = True,
    ) -> dict[str, Any]:
        """Durably claim the single bridged launch for an open, admitted run.

        The run must still be pending and bound to exactly this assignment, task
        and route, with a passing admission for the route's provider. A second
        claim for the same run is refused, so a launch is never resent. With
        `claim` false the same checks run and nothing is written, so a refusal
        can happen before anything is exported or launched.
        """

        timestamp = self._clock_ms()
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        bridge_run_id = f"brg_{secrets.token_hex(16)}"
        with self._lock, self._connection:
            binding = self._connection.execute(
                "SELECT run_id,binding_state,consumed_at_ms,assignment_digest,"
                "task_digest,route_digest,expires_at_ms FROM host_run_bindings "
                "WHERE project_id=? AND handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if binding is None:
                raise LedgerError("host run is unavailable")
            if (
                binding["binding_state"] != "pending"
                or binding["consumed_at_ms"] is not None
                or int(binding["expires_at_ms"]) <= timestamp
            ):
                raise LedgerError("host run is no longer open for dispatch")
            key = self.state.digest_key()
            if (
                str(binding["route_digest"]) != keyed_digest(key, canonical_json(route))
                or str(binding["assignment_digest"]) != keyed_digest(
                    key, canonical_json(validate_assignment_shape(assignment))
                )
                or str(binding["task_digest"]) != keyed_digest(
                    key, canonical_json(validate_task(task))
                )
            ):
                raise LedgerError("dispatch does not match the opened host run")
            run_id = str(binding["run_id"])
            admission = self._run_admission_locked(project_id, run_id)
            if admission["provider"] != route.get("provider"):
                raise LedgerError("host run admission differs from the dispatched provider")
            if self._connection.execute(
                "SELECT 1 FROM records WHERE project_id=? AND record_type='dispatch_intent' "
                "AND json_extract(CAST(content AS TEXT),'$.run_id')=?",
                (project_id, run_id),
            ).fetchone() is not None:
                raise LedgerError("host run was already dispatched")
            if not claim:
                return {"run_id": run_id}
            material = {
                "schema_version": 1,
                "record_type": "dispatch_intent",
                "run_id": run_id,
                "bridge_run_id": bridge_run_id,
                "provider": route["provider"],
                "model": route["model"],
                "effort": route["effort"],
                "deadline_at_ms": timestamp + deadline_seconds * 1000,
                "observed_at_ms": timestamp,
            }
            try:
                record = validate_learning_record(
                    {**material, "record_id": f"dispatch_intent:{content_digest(material)}"}
                )
            except ContractError as exc:
                raise LedgerError("dispatch intent is invalid") from exc
            self._append_checked_locked(
                project_id, record, producer="lifecycle", timestamp=timestamp
            )
        self.state.harden_database_files()
        return {
            "bridge_run_id": bridge_run_id,
            "run_id": run_id,
            "account_fingerprint": admission["account_fingerprint"],
            "deadline_at_ms": record["deadline_at_ms"],
        }

    def host_owner(
        self,
        project_id: str,
        *,
        orchestration_handle: str | None = None,
        run_handle: str | None = None,
    ) -> str:
        """The host client that opened the orchestration owning this handle.

        A run bound before orchestrations existed has no owner record; those were
        only ever opened by the Codex host.
        """

        with self._lock:
            if orchestration_handle is not None:
                row = self._orchestration_for_handle_locked(project_id, orchestration_handle)
                return str(row["client_version"])
            if not isinstance(run_handle, str):
                raise LedgerError("host run handle is invalid")
            binding = self._connection.execute(
                "SELECT orchestration_id FROM host_run_bindings "
                "WHERE project_id=? AND handle_digest=?",
                (project_id, keyed_digest(self.state.digest_key(), run_handle)),
            ).fetchone()
            if binding is None:
                raise LedgerError("host run is unavailable")
            if binding["orchestration_id"] is None:
                return "codex-host-v1"
            row = self._connection.execute(
                "SELECT client_version FROM orchestrations "
                "WHERE project_id=? AND orchestration_id=?",
                (project_id, str(binding["orchestration_id"])),
            ).fetchone()
        if row is None:
            raise LedgerError("host run orchestration is unavailable")
        return str(row["client_version"])

    def record_host_denial(
        self, project_id: str, *, provider: str, reason_code: str
    ) -> None:
        """Store a host-side open denial, as admission denials are stored."""

        timestamp = self._clock_ms()
        decision = {
            "provider": provider, "decision": "deny", "reason_code": reason_code,
            "admission_basis": None, "account_fingerprint": None,
            "evidence_digests": [], "observed_at_ms": timestamp,
        }
        with self._lock, self._connection:
            self._store_admission_locked(
                project_id, decision, run_id=None, timestamp=timestamp
            )
        self.state.harden_database_files()

    def host_run_admission(self, project_id: str, run_handle: str) -> dict[str, Any]:
        """Return the stored admission for a run; time-varying facts are not re-read."""

        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock:
            binding = self._connection.execute(
                "SELECT run_id FROM host_run_bindings "
                "WHERE project_id=? AND handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if binding is None:
                raise LedgerError("host run is unavailable")
            return self._run_admission_locked(project_id, str(binding["run_id"]))

    def observe_host_run(
        self,
        project_id: str,
        *,
        run_handle: str,
        event: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist progress or observation-deadline state without inventing termination."""

        if event not in {"progress", "deadline"}:
            raise LedgerError("host observation event is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        handle_digest = keyed_digest(self.state.digest_key(), run_handle)
        with self._lock, self._connection:
            binding = self._connection.execute(
                "SELECT binding_state,observation_started_at_ms,"
                "observation_deadline_at_ms,last_progress_at_ms,deadline_observed "
                "FROM host_run_bindings WHERE project_id=? AND handle_digest=?",
                (project_id, handle_digest),
            ).fetchone()
            if binding is None or binding["binding_state"] != "pending":
                raise LedgerError("host run is not pending")
            if binding["observation_deadline_at_ms"] is None:
                raise LedgerError("legacy host run requires manual deadline reconciliation")
            deadline = int(binding["observation_deadline_at_ms"])
            if event == "deadline" and timestamp < deadline:
                raise LedgerError("host observation deadline has not elapsed")
            if event == "progress":
                self._connection.execute(
                    "UPDATE host_run_bindings SET last_progress_at_ms=MAX("
                    "last_progress_at_ms,?) WHERE project_id=? AND handle_digest=?",
                    (timestamp, project_id, handle_digest),
                )
            else:
                self._connection.execute(
                    "UPDATE host_run_bindings SET deadline_observed=1 "
                    "WHERE project_id=? AND handle_digest=?",
                    (project_id, handle_digest),
                )
        return {
            "state": "stale" if timestamp >= deadline else "active",
            "observation_started_at_ms": int(binding["observation_started_at_ms"]),
            "observation_deadline_at_ms": deadline,
            "last_progress_at_ms": (
                max(int(binding["last_progress_at_ms"]), timestamp)
                if event == "progress" else int(binding["last_progress_at_ms"])
            ),
            "deadline_observed": bool(binding["deadline_observed"]) or event == "deadline",
        }

    def commit_host_result(
        self,
        project_id: str,
        *,
        assignment: dict[str, Any],
        task: dict[str, Any],
        route: dict[str, Any],
        result: dict[str, Any],
        observation: dict[str, Any],
        execution_configuration: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Derive and atomically admit one result through the trusted host adapter."""

        from .learning_contracts import validate_host_observation

        try:
            checked_observation = validate_host_observation(observation)
        except ContractError as exc:
            raise LedgerError("host observation admission rejected") from exc
        try:
            checked_result = validate_result(result, assignment=assignment)
        except ContractError as exc:
            raise LedgerError("host result admission rejected") from exc
        axes = result_axes(checked_result, attempt=assignment.get("attempt", 1))
        if axes["admission_state"] != "unverified":
            raise LedgerError("host result cannot self-assign admission")
        terminal_state = checked_observation.get("terminal_state")
        if terminal_state is not None and terminal_state != axes["execution_state"]:
            raise LedgerError("host terminal state conflicts with the result")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        trusted_handle = checked_observation.get("run_handle")
        if trusted_handle is not None:
            if axes["execution_state"] == "timed_out" and terminal_state != "timed_out":
                raise LedgerError("timed-out result requires an authoritative host fact")
            handle_digest = keyed_digest(self.state.digest_key(), trusted_handle)
            with self._lock:
                binding = self._connection.execute(
                    "SELECT * FROM host_run_bindings WHERE project_id=? "
                    "AND handle_digest=?",
                    (project_id, handle_digest),
                ).fetchone()
            if (
                binding is None
                or binding["consumed_at_ms"] is not None
                or int(binding["expires_at_ms"]) <= timestamp
                or str(binding["assignment_digest"]) != keyed_digest(
                    self.state.digest_key(),
                    canonical_json(validate_assignment_shape(assignment)),
                )
                or str(binding["task_digest"]) != keyed_digest(
                    self.state.digest_key(), canonical_json(validate_task(task))
                )
                or str(binding["route_digest"]) != keyed_digest(
                    self.state.digest_key(), canonical_json(route)
                )
                or str(binding["configuration_digest"])
                != content_digest(validate_execution_configuration(execution_configuration))
            ):
                raise LedgerError("host run handle does not match this assigned route")
            client_version = str(binding["client_version"])
            checked_observation = {
                "run_id": str(binding["run_id"]),
                "client_version": client_version,
                "result_attempts": checked_observation["result_attempts"],
                **(
                    {"terminal_state": terminal_state}
                    if terminal_state is not None else {}
                ),
            }
            host_evidence = {
                "run_id": checked_observation["run_id"],
                "coverage": "lifecycle_partial",
                "usage": empty_usage(),
                "observed_at_ms": timestamp,
                "no_progress_events": 0,
                "usage_source": "host_observed",
            }
            reconciled = None
        else:
            client_version = checked_observation["client_version"]
        capability = self._client_capabilities.get(client_version)
        if capability is None:
            raise LedgerError("host result client is not in the accepted matrix")
        if trusted_handle is None:
            with self._lock:
                provenance = self._connection.execute(
                    "SELECT 1 FROM run_provenance WHERE project_id=? AND run_id=? "
                    "AND client_version=?",
                    (project_id, checked_observation["run_id"], client_version),
                ).fetchone()
                rows = self._connection.execute(
                    "SELECT source,content FROM records WHERE project_id=? "
                    "AND record_type='event' AND source IN ('hook','app_server')",
                    (project_id,),
                ).fetchall()
            if provenance is None:
                raise LedgerError("host result run has no trusted client binding")
            envelopes = [
                {"source": str(row["source"]), "record": record}
                for row in rows
                if (
                    (record := json.loads(bytes(row["content"]))) ["run_id"]
                    == checked_observation["run_id"]
                )
            ]
            if not envelopes:
                raise LedgerError("host result has no admitted host events")
            reconciled = reconcile_run(envelopes, capability=capability)
            observed_at_ms = max(item["record"]["observed_at_ms"] for item in envelopes)
            host_evidence = {
                "run_id": checked_observation["run_id"],
                "coverage": reconciled["coverage"],
                "usage": reconciled["usage"] or empty_usage(),
                "observed_at_ms": observed_at_ms,
                "no_progress_events": sum(
                    item["record"]["stop_reason"] == "no_progress" for item in envelopes
                ),
                "usage_source": "client_observed",
            }
        # Every result path needs the run's stored passing admission, including
        # collector-bound sessions, whose admission is decided when they bind.
        with self._lock:
            self._run_admission_locked(project_id, checked_observation["run_id"])
        from .learning_engine import build_host_result_records

        evaluation_candidate_digest = None
        evaluation_case_digest = None
        with self._lock:
            assignment_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_assignment'",
                (project_id,),
            ).fetchall()
            bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for row in assignment_rows:
                evaluation_assignment = json.loads(bytes(row["content"]))
                plans = [
                    item for item in evaluation_assignment["trial_plan"]
                    if item["trial_id"] == assignment["assignment_id"]
                ]
                if len(plans) == 1:
                    bindings.append((evaluation_assignment, plans[0]))
            if len(bindings) > 1:
                raise LedgerError("host result matches multiple evaluation assignments")
            if bindings:
                evaluation_assignment, plan = bindings[0]
                candidate_rows = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='candidate'",
                    (project_id,),
                ).fetchall()
                candidates = [
                    value for row in candidate_rows
                    if (value := json.loads(bytes(row["content"]))) ["candidate_digest"]
                    == evaluation_assignment["candidate_digest"]
                ]
                if len(candidates) != 1:
                    raise LedgerError("host result evaluation candidate is unavailable")
                candidate = candidates[0]
                expected_configuration = (
                    candidate["candidate_configuration"]
                    if plan["cohort"] == "candidate"
                    else candidate["champion_configuration"]
                )
                expected_reference = (
                    candidate["candidate_digest"]
                    if plan["cohort"] == "candidate"
                    else candidate["base_digest"]
                )
                case_row = self._connection.execute(
                    "SELECT content,content_digest FROM records WHERE project_id=? "
                    "AND record_id=? AND record_type='evaluation_case'",
                    (project_id, plan["case_id"]),
                ).fetchone()
                if (
                    case_row is None
                    or evaluation_assignment["client_digest"]
                    != execution_configuration["client_digest"]
                    or plan["configuration_digest"]
                    != expected_reference
                    or execution_configuration != expected_configuration
                ):
                    raise LedgerError("host result does not match its evaluation assignment")
                evaluation_candidate_digest = plan["configuration_digest"]
                evaluation_case_digest = str(case_row["content_digest"])

        event, outcome, producer = build_host_result_records(
            assignment=assignment,
            task=task,
            result=checked_result,
            observation=checked_observation,
            host_evidence=host_evidence,
            execution_configuration=execution_configuration,
            digest_key=self.state.digest_key(),
            client_capability=capability,
            evaluation_candidate_digest=evaluation_candidate_digest,
            evaluation_case_digest=evaluation_case_digest,
        )
        if reconciled is not None and reconciled["conflicting"]:
            raise LedgerError("host result events conflict")
        if (
            reconciled is not None
            and axes["execution_state"] == "timed_out"
            and reconciled.get("terminal_stop_reason") != "timeout"
        ):
            raise LedgerError("timed-out result lacks an authoritative host timeout fact")
        if reconciled is not None and reconciled["terminal_status"] != event["status"]:
            raise LedgerError("host result conflicts with the trusted terminal event")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                project = self._connection.execute(
                    "SELECT status FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if not project or project["status"] != "active":
                    raise LedgerError("project binding is not active")
                if trusted_handle is not None:
                    binding = self._connection.execute(
                        "SELECT native_assignment_id FROM host_run_bindings "
                        "WHERE project_id=? AND handle_digest=? "
                        "AND binding_state='pending' AND consumed_at_ms IS NULL "
                        "AND expires_at_ms>?",
                        (project_id, handle_digest, timestamp),
                    ).fetchone()
                    if binding is None:
                        raise LedgerError("host run handle is expired or already consumed")
                    if binding["native_assignment_id"] is None:
                        raise LedgerError("host run is not bound to a native agent")
                    usage_complete = int(
                        all(value is not None for value in outcome["usage"].values())
                    )
                    consumed = self._connection.execute(
                        "UPDATE host_run_bindings SET consumed_at_ms=?,expires_at_ms=?,"
                        "usage_complete=?,binding_state='terminal' "
                        "WHERE project_id=? "
                        "AND handle_digest=? AND binding_state='pending' "
                        "AND consumed_at_ms IS NULL AND expires_at_ms>?",
                        (
                            timestamp,
                            timestamp + DEFAULT_RETENTION_MS,
                            usage_complete,
                            project_id,
                            handle_digest,
                            timestamp,
                        ),
                    )
                    if consumed.rowcount != 1:
                        raise LedgerError("host run handle is expired or already consumed")
                assignment_digest = keyed_digest(
                    self.state.digest_key(),
                    canonical_json(validate_assignment_shape(assignment)),
                )
                for attempt in checked_observation["result_attempts"]:
                    if attempt["status"] != "rejected":
                        continue
                    receipt = self._connection.execute(
                        "SELECT assignment_digest,reason_code,expires_at_ms,consumed_at_ms "
                        "FROM repair_receipts WHERE project_id=? AND receipt_id=?",
                        (project_id, attempt["receipt_id"]),
                    ).fetchone()
                    if (
                        receipt is None
                        or receipt["assignment_digest"] != assignment_digest
                        or receipt["reason_code"] != attempt["reason_code"]
                        or int(receipt["expires_at_ms"]) <= timestamp
                        or receipt["consumed_at_ms"] is not None
                    ):
                        raise LedgerError("result repair receipt is invalid")
                    receipt_consumed = self._connection.execute(
                        "UPDATE repair_receipts SET consumed_at_ms=?,run_id=? "
                        "WHERE project_id=? AND receipt_id=? AND consumed_at_ms IS NULL",
                        (
                            timestamp,
                            checked_observation["run_id"],
                            project_id,
                            attempt["receipt_id"],
                        ),
                    )
                    if receipt_consumed.rowcount != 1:
                        raise LedgerError("result repair receipt is invalid")
                event_result = self._append_checked_locked(
                    project_id, event, producer="host", timestamp=timestamp
                )
                self._validate_evidence_links_locked(project_id, outcome)
                outcome_result = self._append_checked_locked(
                    project_id, outcome, producer="host", timestamp=timestamp
                )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        self.state.harden_database_files()
        return {
            "event": event,
            "outcome": outcome,
            "event_commit": event_result,
            "outcome_commit": outcome_result,
        }

    def _operational_qa_scope_locked(
        self,
        project_id: str,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        host_row = self._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND content_digest=? "
            "AND record_type='outcome' AND source='host'",
            (project_id, host_outcome_digest),
        ).fetchone()
        verifier_row = self._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND content_digest=? "
            "AND record_type='outcome' AND source='host'",
            (project_id, verifier_host_outcome_digest),
        ).fetchone()
        if host_row is None or verifier_row is None:
            raise LedgerError("operational QA requires two durable host outcomes")
        host_outcome = json.loads(bytes(host_row["content"]))
        verifier_host_outcome = json.loads(bytes(verifier_row["content"]))
        binding_rows = self._connection.execute(
            "SELECT run_id,orchestration_id,native_assignment_id "
            "FROM host_run_bindings WHERE project_id=? AND run_id IN (?,?)",
            (
                project_id,
                host_outcome["run_id"],
                verifier_host_outcome["run_id"],
            ),
        ).fetchall()
        if (
            len(binding_rows) != 2
            or any(row["orchestration_id"] is None for row in binding_rows)
            or len({str(row["orchestration_id"]) for row in binding_rows}) != 1
            or any(row["native_assignment_id"] is None for row in binding_rows)
            or len({str(row["native_assignment_id"]) for row in binding_rows}) != 2
        ):
            raise LedgerError(
                "operational QA requires two distinct native agents in one orchestration"
            )
        orchestration_id = str(binding_rows[0]["orchestration_id"])
        orchestration = self._connection.execute(
            "SELECT status FROM orchestrations WHERE project_id=? AND orchestration_id=?",
            (project_id, orchestration_id),
        ).fetchone()
        if orchestration is None or orchestration["status"] != "active":
            raise LedgerError("operational QA requires an active orchestration")
        return host_outcome, verifier_host_outcome, orchestration_id

    def validate_operational_qa_scope(
        self,
        project_id: str,
        *,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
    ) -> dict[str, Any]:
        """Validate the two exact runs before executing project-approved checks."""

        with self._lock:
            _, _, orchestration_id = self._operational_qa_scope_locked(
                project_id, host_outcome_digest, verifier_host_outcome_digest
            )
        return {"ready": True, "orchestration_id": orchestration_id}

    def issue_operational_check_receipt(
        self,
        project_id: str,
        *,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
        check_report: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Bind a trusted runner's closed report to two runs with a single-use receipt."""

        from .learning_engine import _check_report_digests

        if not isinstance(check_report, dict) or set(check_report) != {
            "deterministic_checks", "security_checks", "observed_at_ms"
        }:
            raise LedgerError("operational check report is invalid")
        try:
            _, deterministic_pass = _check_report_digests(
                check_report["deterministic_checks"], "deterministic_checks"
            )
            _, security_pass = _check_report_digests(
                check_report["security_checks"], "security_checks"
            )
        except ContractError as exc:
            raise LedgerError("operational check report is invalid") from exc
        observed_at_ms = check_report["observed_at_ms"]
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        if (
            not isinstance(observed_at_ms, int)
            or isinstance(observed_at_ms, bool)
            or observed_at_ms > timestamp + 1_000
        ):
            raise LedgerError("operational check report time is invalid")
        receipt_id = f"oqc_{secrets.token_hex(24)}"
        with self._lock, self._connection:
            host, verifier, orchestration_id = self._operational_qa_scope_locked(
                project_id, host_outcome_digest, verifier_host_outcome_digest
            )
            if observed_at_ms < max(
                int(host["observed_at_ms"]), int(verifier["observed_at_ms"])
            ):
                raise LedgerError("operational check report predates its runs")
            self._connection.execute(
                "DELETE FROM operational_check_receipts "
                "WHERE project_id=? AND expires_at_ms<=?",
                (project_id, timestamp),
            )
            pending = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM operational_check_receipts "
                    "WHERE project_id=? AND consumed_at_ms IS NULL",
                    (project_id,),
                ).fetchone()[0]
            )
            if pending >= MAX_PENDING_OPERATIONAL_CHECKS:
                raise LedgerError("pending operational check capacity is exhausted")
            report_digest = content_digest(check_report)
            self._connection.execute(
                "INSERT INTO operational_check_receipts VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                (
                    project_id,
                    receipt_id,
                    report_digest,
                    host_outcome_digest,
                    verifier_host_outcome_digest,
                    orchestration_id,
                    timestamp,
                    timestamp + OPERATIONAL_CHECK_TTL_MS,
                    canonical_json(check_report),
                ),
            )
        self.state.harden_database_files()
        return {
            "check_receipt": receipt_id,
            "check_report_digest": report_digest,
            "checks_passed": deterministic_pass and security_pass,
            "expires_at_ms": timestamp + OPERATIONAL_CHECK_TTL_MS,
        }

    def commit_operational_qa(
        self,
        project_id: str,
        *,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
        evaluator_assignment: dict[str, Any],
        evaluator_result: dict[str, Any],
        check_receipt: str,
        verifier_assignment: dict[str, Any],
        verifier_result: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Atomically admit routine independent QA and its superseding outcome."""

        if not isinstance(check_receipt, str) or not re.fullmatch(
            r"oqc_[0-9a-f]{48}", check_receipt
        ):
            raise LedgerError("operational check receipt is invalid")
        current_time = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock:
            host_outcome, verifier_host_outcome, orchestration_id = (
                self._operational_qa_scope_locked(
                    project_id, host_outcome_digest, verifier_host_outcome_digest
                )
            )
            receipt = self._connection.execute(
                "SELECT report_digest,host_outcome_digest,"
                "verifier_host_outcome_digest,orchestration_id,expires_at_ms,"
                "consumed_at_ms,report FROM operational_check_receipts "
                "WHERE project_id=? AND receipt_id=?",
                (
                    project_id,
                    check_receipt,
                ),
            ).fetchone()
        if (
            receipt is None
            or receipt["host_outcome_digest"] != host_outcome_digest
            or receipt["verifier_host_outcome_digest"] != verifier_host_outcome_digest
            or receipt["orchestration_id"] != orchestration_id
            or int(receipt["expires_at_ms"]) <= current_time
            or receipt["consumed_at_ms"] is not None
        ):
            raise LedgerError("operational check receipt is unavailable")
        check_report = json.loads(bytes(receipt["report"]))
        if content_digest(check_report) != receipt["report_digest"]:
            raise LedgerError("operational check receipt report is invalid")
        from .learning_engine import build_operational_qa_records

        try:
            qa, outcome = build_operational_qa_records(
                digest_key=self.state.digest_key(),
                host_outcome=host_outcome,
                verifier_host_outcome=verifier_host_outcome,
                evaluator_assignment=evaluator_assignment,
                evaluator_result=evaluator_result,
                check_report=check_report,
                verifier_assignment=verifier_assignment,
                verifier_result=verifier_result,
            )
        except ContractError as exc:
            raise LedgerError("operational QA admission rejected") from exc
        qa_digest = content_digest(qa)
        timestamp = int(qa["observed_at_ms"])
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                consumed = self._connection.execute(
                    "UPDATE operational_check_receipts SET consumed_at_ms=? "
                    "WHERE project_id=? AND receipt_id=? AND consumed_at_ms IS NULL "
                    "AND expires_at_ms>?",
                    (current_time, project_id, check_receipt, current_time),
                )
                if consumed.rowcount != 1:
                    raise LedgerError("operational check receipt is unavailable")
                existing = self._connection.execute(
                    "SELECT qa_digest FROM operational_qa WHERE project_id=? "
                    "AND host_outcome_digest=?",
                    (project_id, host_outcome_digest),
                ).fetchone()
                if existing is not None:
                    raise LedgerError("host outcome already has operational QA")
                self._connection.execute(
                    "INSERT INTO operational_qa VALUES(?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        qa["record_id"],
                        qa_digest,
                        host_outcome_digest,
                        verifier_host_outcome_digest,
                        int(qa["accepted"]),
                        timestamp,
                        canonical_json(qa),
                    ),
                )
                self._validate_evidence_links_locked(project_id, outcome)
                outcome_commit = self._append_checked_locked(
                    project_id, outcome, producer="verifier", timestamp=timestamp
                )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        self.state.harden_database_files()
        return {
            "qa": qa,
            "outcome": outcome,
            "stored": True,
            "duplicate": False,
            "outcome_commit": outcome_commit,
            "attention_required": not qa["accepted"],
        }

    def prepare_verified_evaluation_outcome(
        self,
        project_id: str,
        *,
        evaluation_assignment_digest: str,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
        evaluator_assignment: dict[str, Any],
        evaluator_result: dict[str, Any],
        check_report: dict[str, Any],
        verifier_assignment: dict[str, Any],
        verifier_result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Prepare exact QA bytes from two durable host-bound executions."""

        with self._lock:
            assignment_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND content_digest=? "
                "AND record_type='evaluation_assignment'",
                (project_id, evaluation_assignment_digest),
            ).fetchone()
            host_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND content_digest=? "
                "AND record_type='outcome' AND source='host'",
                (project_id, host_outcome_digest),
            ).fetchone()
            verifier_host_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND content_digest=? "
                "AND record_type='outcome' AND source='host'",
                (project_id, verifier_host_outcome_digest),
            ).fetchone()
            if (
                assignment_row is None
                or host_row is None
                or verifier_host_row is None
            ):
                raise LedgerError(
                    "verified QA requires durable assignment, evaluator, and verifier runs"
                )
            assignment_record = json.loads(bytes(assignment_row["content"]))
            planned = [
                item for item in assignment_record["trial_plan"]
                if item["trial_id"] == evaluator_assignment.get("assignment_id")
            ]
            if len(planned) != 1:
                raise LedgerError("verified QA trial is not in the durable assignment")
            case_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=? "
                "AND record_type='evaluation_case'",
                (project_id, planned[0]["case_id"]),
            ).fetchone()
            if case_row is None:
                raise LedgerError("verified QA evaluation case is unavailable")
            case_record = json.loads(bytes(case_row["content"]))
            host_record = json.loads(bytes(host_row["content"]))
            verifier_host_record = json.loads(bytes(verifier_host_row["content"]))
        from .learning_engine import build_verified_qa_records

        try:
            qa, outcome = build_verified_qa_records(
                digest_key=self.state.digest_key(),
                host_outcome=host_record,
                verifier_host_outcome=verifier_host_record,
                evaluation_assignment=assignment_record,
                evaluation_case=case_record,
                evaluator_assignment=evaluator_assignment,
                evaluator_result=evaluator_result,
                check_report=check_report,
                verifier_assignment=verifier_assignment,
                verifier_result=verifier_result,
            )
        except ContractError as exc:
            raise LedgerError("verified QA evidence admission rejected") from exc
        return qa, outcome

    def commit_verified_evaluation_outcome(
        self,
        project_id: str,
        *,
        evaluation_assignment_digest: str,
        host_outcome_digest: str,
        verifier_host_outcome_digest: str,
        evaluator_assignment: dict[str, Any],
        evaluator_result: dict[str, Any],
        check_report: dict[str, Any],
        verifier_assignment: dict[str, Any],
        verifier_result: dict[str, Any],
        signature: bytes,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Commit exact owner-attested QA backed by evaluator and verifier runs."""

        qa, outcome = self.prepare_verified_evaluation_outcome(
            project_id,
            evaluation_assignment_digest=evaluation_assignment_digest,
            host_outcome_digest=host_outcome_digest,
            verifier_host_outcome_digest=verifier_host_outcome_digest,
            evaluator_assignment=evaluator_assignment,
            evaluator_result=evaluator_result,
            check_report=check_report,
            verifier_assignment=verifier_assignment,
            verifier_result=verifier_result,
        )
        from .learning_engine import OwnerGrantAuthority

        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        if not authority.verify_qa_attestation(project_id, qa, signature):
            raise LedgerError("verified QA attestation is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            qa_result = self._append_checked_locked(
                project_id, qa, producer="verifier", timestamp=timestamp
            )
            self._validate_evidence_links_locked(project_id, outcome)
            outcome_result = self._append_checked_locked(
                project_id, outcome, producer="evaluator", timestamp=timestamp
            )
            receipt = self._connection.execute(
                "SELECT signature FROM owner_attestations "
                "WHERE project_id=? AND subject_record_id=?",
                (project_id, qa["record_id"]),
            ).fetchone()
            if receipt and bytes(receipt["signature"]) != signature:
                raise LedgerError("verified QA attestation conflicts")
            self._connection.execute(
                "INSERT OR IGNORE INTO owner_attestations VALUES(?,?,?,?)",
                (project_id, qa["record_id"], signature, timestamp),
            )
        self.state.harden_database_files()
        return {
            "qa_evidence": qa,
            "outcome": outcome,
            "qa_commit": qa_result,
            "outcome_commit": outcome_result,
        }

    def commit_evidence_review(
        self,
        project_id: str,
        *,
        subject_digests: list[str],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record a deterministic post-population review of verified subjects."""

        subjects = sorted(subject_digests)
        if not subjects or len(subjects) != len(set(subjects)):
            raise LedgerError("evidence review subjects are invalid")
        with self._lock:
            rows = self._connection.execute(
                "SELECT record_type,content,content_digest FROM records "
                "WHERE project_id=?",
                (project_id,),
            ).fetchall()
            by_digest = {
                str(row["content_digest"]): (
                    str(row["record_type"]), json.loads(bytes(row["content"]))
                )
                for row in rows
            }
            qa_rows = {
                str(row["content_digest"]): json.loads(bytes(row["content"]))
                for row in rows
                if row["record_type"] == "qa_evidence"
            }
            attestations = {
                str(row[0]) for row in self._connection.execute(
                    "SELECT subject_record_id FROM owner_attestations WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            }
            for digest in subjects:
                stored = by_digest.get(digest)
                if stored is None or stored[0] not in {"evaluation_trial", "outcome"}:
                    raise LedgerError("evidence review subject is unavailable")
                record = stored[1]
                outcome = record
                if stored[0] == "evaluation_trial":
                    linked = by_digest.get(record["outcome_digest"])
                    if linked is None or linked[0] != "outcome":
                        raise LedgerError("evidence review trial outcome is unavailable")
                    outcome = linked[1]
                linked_qa = [
                    qa for qa_digest, qa in qa_rows.items()
                    if qa_digest in outcome["evidence_digests"]
                ]
                if (
                    len(linked_qa) != 1
                    or linked_qa[0]["record_id"] not in attestations
                ):
                    raise LedgerError("evidence review subject lacks attested QA")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        material = {
            "schema_version": 1,
            "record_type": "evidence_review",
            "subject_digests": subjects,
            "accepted": True,
            "observed_at_ms": timestamp,
        }
        checked = validate_learning_record(
            {
                **material,
                "record_id": f"evidence_review:{content_digest(material)}",
            }
        )
        with self._lock, self._connection:
            result = self._append_checked_locked(
                project_id, checked, producer="verifier", timestamp=timestamp
            )
        self.state.harden_database_files()
        return {"record": checked, **result}

    def commit_owner_record(
        self,
        project_id: str,
        *,
        record: dict[str, Any],
        signature: bytes,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Verify and atomically store signed owner feedback or calibration."""

        try:
            checked = validate_learning_record(record)
        except ContractError as exc:
            raise LedgerError("record admission rejected") from exc
        if checked["record_type"] not in {
            "feedback", "grader_calibration", "billing_attestation"
        }:
            raise LedgerError("authenticated owner record type is unsupported")
        from .learning_engine import OwnerGrantAuthority

        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        if not authority.verify_owner_record(project_id, checked, signature):
            raise LedgerError("owner record signature is invalid")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            self._validate_evidence_links_locked(project_id, checked)
            result = self._append_checked_locked(
                project_id, checked, producer="owner", timestamp=timestamp
            )
            receipt = self._connection.execute(
                "SELECT signature FROM owner_attestations "
                "WHERE project_id=? AND subject_record_id=?",
                (project_id, checked["record_id"]),
            ).fetchone()
            if receipt and bytes(receipt["signature"]) != signature:
                raise LedgerError("owner record attestation conflicts")
            self._connection.execute(
                "INSERT OR IGNORE INTO owner_attestations VALUES(?,?,?,?)",
                (project_id, checked["record_id"], signature, timestamp),
            )
        self.state.harden_database_files()
        return result

    def _validate_evidence_links_locked(
        self, project_id: str, checked: dict[str, Any]
    ) -> None:
        if checked["record_type"] == "candidate":
            evidence_rows = self._connection.execute(
                "SELECT record_type,content,content_digest FROM records "
                "WHERE project_id=?",
                (project_id,),
            ).fetchall()
            evidence = {
                str(row["content_digest"]): (
                    str(row["record_type"]), json.loads(bytes(row["content"]))
                )
                for row in evidence_rows
            }
            if not checked["evidence_digests"] or any(
                digest not in evidence for digest in checked["evidence_digests"]
            ):
                raise LedgerError("candidate evidence is not durable project evidence")
            configuration_fields = set(checked["champion_configuration"])
            if not any(
                kind == "outcome"
                and record["task_slice"] == checked["task_slice"]
                and {
                    field: record[field] for field in configuration_fields
                } == checked["champion_configuration"]
                for digest in checked["evidence_digests"]
                for kind, record in [evidence[digest]]
            ):
                raise LedgerError(
                    "candidate champion lacks matching durable outcome evidence"
                )
            if self._candidate_evidence_invalidated_locked(project_id, checked):
                raise LedgerError("candidate evidence was invalidated by owner feedback")
            return
        if checked["record_type"] == "feedback":
            outcomes = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type='outcome'",
                (project_id,),
            ).fetchall()
            if not any(
                json.loads(bytes(row["content"]))["run_id"] == checked["run_id"]
                for row in outcomes
            ):
                raise LedgerError("owner feedback run is not admitted")
            return
        if checked["record_type"] == "outcome":
            event_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type='event' "
                "AND source='host'",
                (project_id,),
            ).fetchall()
            linked_events = [
                event
                for row in event_rows
                if (
                    (event := json.loads(bytes(row["content"]))) ["run_id"]
                    == checked["run_id"]
                    and event["assignment_id"] == checked["assignment_id"]
                    and event["model"] == checked["model"]
                    and event["effort"] == checked["effort"]
                    and event["role"] == checked["role"]
                    and event["task_slice"] == checked["task_slice"]
                    and event["topology"] == checked["topology"]
                    and event["configuration_digest"]
                    == checked["configuration_digest"]
                    and event["coverage"] == checked["coverage"]
                )
            ]
            terminal_event = {
                "accepted": ({"item_completed", "turn_completed"}, {"completed"}),
                "rejected": ({"item_completed", "turn_completed"}, {"completed"}),
                "unverified": ({"item_completed", "turn_completed"}, {"completed"}),
                "failed": ({"turn_failed"}, {"failed"}),
                "interrupted": ({"turn_interrupted"}, {"interrupted"}),
                "cancelled": ({"turn_interrupted"}, {"cancelled"}),
            }[checked["status"]]
            qa_rows = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_type='qa_evidence' AND source='verifier'",
                (project_id,),
            ).fetchall()
            verified_host_digest = None
            for row in qa_rows:
                qa_digest = str(row["content_digest"])
                qa_record = json.loads(bytes(row["content"]))
                if qa_digest in checked["evidence_digests"]:
                    verified_host_digest = qa_record["host_outcome_digest"]
                    break
            verified_host = None
            if verified_host_digest is not None:
                verified_host = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND content_digest=? AND record_type='outcome' AND source='host'",
                    (project_id, verified_host_digest),
                ).fetchone()
            linked = any(
                (
                    event["event_code"] in terminal_event[0]
                    and event["status"] in terminal_event[1]
                    and event["item_digest"] is not None
                    and event["item_digest"] in checked["evidence_digests"]
                    and (
                        event["usage"] == checked["usage"]
                        or verified_host is not None
                    )
                )
                for event in linked_events
            )
            if not linked:
                raise LedgerError("outcome lacks completed host result provenance")
            if checked["evaluation_candidate_digest"] is not None:
                case_row = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='evaluation_case' AND content_digest=?",
                    (project_id, checked["evaluation_case_digest"]),
                ).fetchone()
                if case_row:
                    evaluation_case = json.loads(bytes(case_row["content"]))
                    if evaluation_case["corpus_state"] == "promotion_holdout":
                        assignment_rows = self._connection.execute(
                            "SELECT content FROM records WHERE project_id=? "
                            "AND record_type='evaluation_assignment'",
                            (project_id,),
                        ).fetchall()
                        matching_assignments = []
                        for row in assignment_rows:
                            assignment = json.loads(bytes(row["content"]))
                            plans = [
                                item for item in assignment["trial_plan"]
                                if item["trial_id"] == checked["assignment_id"]
                                and item["case_id"] == evaluation_case["record_id"]
                                and item["configuration_digest"]
                                == checked["evaluation_candidate_digest"]
                            ]
                            if assignment["client_digest"] == checked["client_digest"] and len(plans) == 1:
                                matching_assignments.append((assignment, plans[0]))
                        if len(matching_assignments) != 1:
                            raise LedgerError(
                                "evaluation outcome is not in a durable assignment"
                            )
                        assigned, planned = matching_assignments[0]
                        candidate_rows = self._connection.execute(
                            "SELECT content FROM records WHERE project_id=? "
                            "AND record_type='candidate'",
                            (project_id,),
                        ).fetchall()
                        candidates = [
                            json.loads(bytes(row["content"])) for row in candidate_rows
                        ]
                        candidates = [
                            candidate for candidate in candidates
                            if candidate["candidate_digest"]
                            == assigned["candidate_digest"]
                        ]
                        from .learning_engine import candidate_matches_outcome
                        if len(candidates) != 1 or not candidate_matches_outcome(
                            candidates[0], planned["cohort"], checked
                        ):
                            raise LedgerError(
                                "evaluation outcome does not match its candidate configuration"
                            )
                        existing_outcomes = self._connection.execute(
                            "SELECT content FROM records WHERE project_id=? "
                            "AND record_type='outcome' AND source='evaluator'",
                            (project_id,),
                        ).fetchall()
                        if any(
                            json.loads(bytes(row["content"]))["assignment_id"]
                            == checked["assignment_id"]
                            and json.loads(bytes(row["content"]))["record_id"]
                            != checked["record_id"]
                            for row in existing_outcomes
                        ):
                            raise LedgerError("evaluation assignment already has an outcome")
            return
        if checked["record_type"] == "evaluation_case":
            source = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=? "
                "AND record_type='outcome'",
                (project_id, checked["source_outcome_id"]),
            ).fetchone()
            if not source:
                raise LedgerError("evaluation case source outcome is missing")
            source_record = json.loads(bytes(source["content"]))
            if (
                source_record["task_slice"] != checked["task_slice"]
                or source_record["fixture_digest"] != checked["fixture_digest"]
            ):
                raise LedgerError("evaluation case differs from its source outcome")
            if source_record["run_id"] in self._owner_invalidated_run_ids_locked(
                project_id
            ):
                raise LedgerError("evaluation case source was invalidated by owner feedback")
            previous_id = checked["supersedes_case_id"]
            if previous_id is None:
                if checked["corpus_state"] != "development":
                    raise LedgerError("controlled evaluation case must supersede a development case")
            else:
                previous = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? AND record_id=? "
                    "AND record_type='evaluation_case'",
                    (project_id, previous_id),
                ).fetchone()
                if not previous:
                    raise LedgerError("superseded evaluation case is missing")
                previous_record = json.loads(bytes(previous["content"]))
                immutable = {
                    "source_outcome_id", "task_slice", "fixture_digest",
                    "criteria_digest", "lineage_digest",
                }
                if any(previous_record[field] != checked[field] for field in immutable):
                    raise LedgerError("evaluation case revision changes immutable evidence")
                if previous_record["similarity_group_digest"] not in {
                    None, checked["similarity_group_digest"]
                }:
                    raise LedgerError("evaluation case revision changes its similarity group")
            related_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_case' AND record_id<>?",
                (project_id, previous_id or checked["record_id"]),
            ).fetchall()
            related = [json.loads(bytes(row["content"])) for row in related_rows]
            controlled = [
                record for record in related
                if record["corpus_state"] != "development"
                and record["record_id"] != checked["record_id"]
            ]
            if previous_id is not None and any(
                record["supersedes_case_id"] == previous_id for record in controlled
            ):
                raise LedgerError("development case already entered a controlled corpus")
            split_key = (
                checked["source_outcome_id"], checked["fixture_digest"],
                checked["criteria_digest"], checked["lineage_digest"],
            )
            if checked["corpus_state"] != "development" and any(
                (
                    record["source_outcome_id"], record["fixture_digest"],
                    record["criteria_digest"], record["lineage_digest"],
                ) == split_key
                for record in controlled
            ):
                raise LedgerError("evaluation evidence already belongs to a controlled corpus")
            same_group = [
                record for record in related
                if record["similarity_group_digest"] is not None
                if record["similarity_group_digest"]
                == checked["similarity_group_digest"]
            ]
            if any(
                record["corpus_state"] != checked["corpus_state"]
                for record in same_group
            ):
                raise LedgerError(
                    "reviewed similarity group already exists in the project corpus"
                )
            return
        if checked["record_type"] == "evaluation_assignment":
            candidate_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='candidate'",
                (project_id,),
            ).fetchall()
            matching_candidates = [
                row for row in candidate_rows
                if json.loads(bytes(row["content"]))["candidate_digest"]
                == checked["candidate_digest"]
            ]
            if len(matching_candidates) != 1:
                raise LedgerError("evaluation assignment candidate is missing")
            candidate_row = matching_candidates[0]
            candidate = json.loads(bytes(candidate_row["content"]))
            if self._candidate_evidence_invalidated_locked(project_id, candidate):
                raise LedgerError("evaluation candidate was invalidated by owner feedback")
            if candidate["task_slice"] != checked["task_slice"]:
                raise LedgerError("evaluation assignment task slice does not match")
            supported_paths = {"routing.model_effort"}
            if any(
                change["path_code"] not in supported_paths
                for change in candidate["change_manifest"]
            ):
                raise LedgerError(
                    "candidate target lacks exact evaluation configuration binding"
                )
            capability = self._client_capabilities.get(checked["client_version"])
            if (
                capability is None
                or content_digest(capability) != checked["client_digest"]
                or capability.get("app_server_coverage") != "end_to_end_complete"
                or capability.get("child_usage") is not True
                or capability.get("ordering_proven") is not True
            ):
                raise LedgerError("evaluation assignment client is not accepted")
            case_rows = self._connection.execute(
                "SELECT record_id,content,content_digest,source_sequence FROM records "
                "WHERE project_id=? AND record_type='evaluation_case'",
                (project_id,),
            ).fetchall()
            by_id = {str(row["record_id"]): row for row in case_rows}
            selected = [by_id.get(case_id) for case_id in checked["case_ids"]]
            if any(row is None for row in selected):
                raise LedgerError("evaluation assignment case is missing")
            selected_rows = [row for row in selected if row is not None]
            selected_cases = [json.loads(bytes(row["content"])) for row in selected_rows]
            source_run_ids = {
                str(row["record_id"]): json.loads(bytes(row["content"]))["run_id"]
                for row in self._connection.execute(
                    "SELECT record_id,content FROM records WHERE project_id=? "
                    "AND record_type='outcome'",
                    (project_id,),
                ).fetchall()
            }
            invalidated = self._owner_invalidated_run_ids_locked(project_id)
            if (
                [str(row["content_digest"]) for row in selected_rows]
                != checked["case_digests"]
                or any(
                    case["corpus_state"] not in {"promotion_holdout", "canary"}
                    or not case["reviewed"] or not case["frozen"]
                    or case["task_slice"] != checked["task_slice"]
                    for case in selected_cases
                )
                or len({case["corpus_state"] for case in selected_cases}) != 1
                or any(
                    source_run_ids.get(case["source_outcome_id"]) in invalidated
                    for case in selected_cases
                )
                or any(
                    int(row["source_sequence"]) >= int(candidate_row["source_sequence"])
                    for row in selected_rows
                )
            ):
                raise LedgerError(
                    "evaluation cases must be frozen before candidate generation"
                )
            from .learning_engine import deduplicate_cases
            duplicates = deduplicate_cases(selected_cases)
            if any(duplicates.values()):
                raise LedgerError("evaluation assignment contains corpus leakage")
            expected_configurations = {
                "candidate": candidate["candidate_digest"],
                "champion": candidate["base_digest"],
            }
            if any(
                item["configuration_digest"] != expected_configurations[item["cohort"]]
                for item in checked["trial_plan"]
            ):
                raise LedgerError("evaluation assignment configuration is invalid")
            return
        if checked["record_type"] == "evaluation_trial":
            case_row = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_id=? AND record_type='evaluation_case'",
                (project_id, checked["case_id"]),
            ).fetchone()
            outcome_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND content_digest=? "
                "AND record_type='outcome' AND source='evaluator'",
                (project_id, checked["outcome_digest"]),
            ).fetchone()
            capability = self._client_capabilities.get(checked["client_version"])
            assignment_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_assignment' AND content_digest=?",
                (project_id, checked["evaluation_assignment_digest"]),
            ).fetchone()
            if not case_row or not outcome_row or capability is None or not assignment_row:
                raise LedgerError("evaluation trial provenance is missing")
            case = json.loads(bytes(case_row["content"]))
            outcome = json.loads(bytes(outcome_row["content"]))
            if outcome["run_id"] in self._owner_invalidated_run_ids_locked(project_id):
                raise LedgerError("evaluation outcome was invalidated by owner feedback")
            assignment = json.loads(bytes(assignment_row["content"]))
            plan = [
                item for item in assignment["trial_plan"]
                if item["trial_id"] == checked["trial_id"]
            ]
            if len(plan) != 1:
                raise LedgerError("evaluation trial is not in its assignment")
            expected = {
                "case_id": plan[0]["case_id"],
                "cohort": plan[0]["cohort"],
                "task_slice": case["task_slice"],
                "configuration_digest": plan[0]["configuration_digest"],
                "coverage": outcome["coverage"],
                "deterministic_pass": outcome["deterministic_pass"],
                "security_pass": outcome["security_pass"],
                "required_criteria_pass": outcome["required_criteria_passed"]
                == outcome["required_criteria_total"],
                "quality_micros": outcome["quality_micros"],
                "terminal_failure": outcome["terminal_failure"],
                "repairs": outcome["repairs"],
                "input_tokens": outcome["usage"]["input_tokens"],
                "output_tokens": outcome["usage"]["output_tokens"],
                "model_calls": outcome["usage"]["model_calls"],
                "client_digest": content_digest(capability),
                "host_configuration_digest": outcome["configuration_digest"],
            }
            if (
                outcome["evaluation_case_digest"] != str(case_row["content_digest"])
                or outcome["assignment_id"] != checked["trial_id"]
                or any(checked[field] != value for field, value in expected.items())
            ):
                raise LedgerError("evaluation trial differs from persisted evidence")
            existing_trials = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_trial'",
                (project_id,),
            ).fetchall()
            if any(
                json.loads(bytes(row["content"]))["evaluation_assignment_digest"]
                == checked["evaluation_assignment_digest"]
                and json.loads(bytes(row["content"]))["trial_id"] == checked["trial_id"]
                and json.loads(bytes(row["content"]))["record_id"] != checked["record_id"]
                for row in existing_trials
            ):
                raise LedgerError("evaluation assignment trial already exists")
            return

        if checked["record_type"] == "promotion":
            self._validate_promotion_evidence_locked(project_id, checked)
            return
        if checked["record_type"] not in {"comparison", "canary_report"}:
            return
        candidate_rows = self._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='candidate'",
            (project_id,),
        ).fetchall()
        candidates = [json.loads(bytes(row["content"])) for row in candidate_rows]
        matching_candidates = [
            record for record in candidates
            if record["candidate_digest"] == checked["candidate_digest"]
        ]
        if len(matching_candidates) != 1:
            raise LedgerError("evaluation evidence candidate is not in this project")
        persisted_candidate = matching_candidates[0]
        if self._candidate_evidence_invalidated_locked(
            project_id, persisted_candidate
        ):
            raise LedgerError("candidate was invalidated by owner feedback")
        review_rows = self._connection.execute(
            "SELECT content,source_sequence FROM records WHERE project_id=? AND content_digest=? "
            "AND record_type='evidence_review' AND source='verifier'",
            (project_id, checked["independent_review_digest"]),
        ).fetchall()
        valid_reviews: list[dict[str, Any]] = []
        for row in review_rows:
            review = json.loads(bytes(row["content"]))
            review["_source_sequence"] = int(row["source_sequence"])
            if review.get("accepted") is True:
                valid_reviews.append(review)
        if not valid_reviews:
            raise LedgerError("independent verification evidence is missing")
        if checked["record_type"] == "comparison":
            assignment_row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_assignment' AND content_digest=?",
                (project_id, checked["evaluation_assignment_digest"]),
            ).fetchone()
            if not assignment_row:
                raise LedgerError("comparison evaluation assignment is missing")
            assignment = json.loads(bytes(assignment_row["content"]))
            if (
                assignment["candidate_digest"] != checked["candidate_digest"]
                or assignment["case_ids"] != checked["case_ids"]
                or assignment["case_digests"] != checked["case_digests"]
                or assignment["client_version"] != checked["client_version"]
                or assignment["client_digest"] != checked["client_digest"]
            ):
                raise LedgerError("comparison differs from its evaluation assignment")
            capability = self._client_capabilities.get(checked["client_version"])
            if (
                capability is None
                or content_digest(capability) != checked["client_digest"]
            ):
                raise LedgerError("comparison client is not in the accepted matrix")
            trial_rows = self._connection.execute(
                "SELECT content,content_digest,source_sequence FROM records WHERE project_id=? "
                "AND record_type='evaluation_trial'",
                (project_id,),
            ).fetchall()
            persisted_trials = {
                str(row["content_digest"]): json.loads(bytes(row["content"]))
                for row in trial_rows
            }
            trial_sequences = {
                str(row["content_digest"]): int(row["source_sequence"])
                for row in trial_rows
            }
            if any(digest not in persisted_trials for digest in checked["trial_digests"]):
                raise LedgerError("comparison trials are not durable project evidence")
            assigned_trials = [
                record for record in persisted_trials.values()
                if record["evaluation_assignment_digest"]
                == checked["evaluation_assignment_digest"]
            ]
            expected_trial_ids = sorted(
                item["trial_id"] for item in assignment["trial_plan"]
            )
            if (
                sorted(record["trial_id"] for record in assigned_trials)
                != expected_trial_ids
                or sorted(content_digest(record) for record in assigned_trials)
                != checked["trial_digests"]
            ):
                raise LedgerError("comparison does not contain the complete assigned population")
            if not any(
                review["subject_digests"] == sorted(checked["trial_digests"])
                and review["_source_sequence"]
                > max(trial_sequences[digest] for digest in checked["trial_digests"])
                for review in valid_reviews
            ):
                raise LedgerError(
                    "independent review predates or omits comparison trials"
                )
            if checked["grader_digest"] is not None:
                calibration = self._connection.execute(
                    "SELECT records.content FROM records JOIN owner_attestations "
                    "ON owner_attestations.project_id=records.project_id "
                    "AND owner_attestations.subject_record_id=records.record_id "
                    "WHERE records.project_id=? "
                    "AND records.record_type='grader_calibration' "
                    "AND records.content_digest=? AND records.source='owner'",
                    (project_id, checked["grader_digest"]),
                ).fetchone()
                if (
                    not calibration
                    or json.loads(bytes(calibration["content"]))["ready"] is not True
                ):
                    raise LedgerError(
                        "comparison grader calibration is missing or not ready"
                    )
            rows = self._connection.execute(
                "SELECT record_id,content_digest FROM records WHERE project_id=? "
                "AND record_type='evaluation_case'",
                (project_id,),
            ).fetchall()
            present = {
                str(row["record_id"]): str(row["content_digest"]) for row in rows
            }
            expected_cases = dict(zip(checked["case_ids"], checked["case_digests"]))
            if any(present.get(case_id) != digest for case_id, digest in expected_cases.items()):
                raise LedgerError("comparison cases are not in this project")
            selected_trials = [
                persisted_trials[digest] for digest in checked["trial_digests"]
            ]
            selected_outcomes = []
            for trial in selected_trials:
                row = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='outcome' AND content_digest=?",
                    (project_id, trial["outcome_digest"]),
                ).fetchone()
                if not row:
                    raise LedgerError("comparison outcome evidence is missing")
                selected_outcomes.append(
                    (trial["cohort"], json.loads(bytes(row["content"])))
                )
            from .learning_engine import candidate_matches_outcome
            if any(
                not candidate_matches_outcome(persisted_candidate, cohort, outcome)
                for cohort, outcome in selected_outcomes
            ):
                raise LedgerError("comparison cohort does not match candidate configuration")
            configuration_fields = {
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                "client_digest",
            }
            changed_fields: set[str] = set()
            for change in persisted_candidate["change_manifest"]:
                changed_fields.update(
                    {
                        "routing.model_effort": {"model", "effort"},
                    }.get(change["path_code"], set())
                )
            unchanged = configuration_fields - changed_fields
            normalized = {
                content_digest({field: outcome[field] for field in sorted(unchanged)})
                for _, outcome in selected_outcomes
            }
            cohort_vectors = {
                cohort: {
                    outcome["configuration_digest"]
                    for item_cohort, outcome in selected_outcomes
                    if item_cohort == cohort
                }
                for cohort in ("champion", "candidate")
            }
            if len(normalized) != 1 or any(
                len(vectors) != 1 for vectors in cohort_vectors.values()
            ):
                raise LedgerError("comparison cohorts contain configuration confounding")
            selected_cases = []
            for case_id, digest in expected_cases.items():
                row = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? AND record_id=? "
                    "AND record_type='evaluation_case' AND content_digest=?",
                    (project_id, case_id, digest),
                ).fetchone()
                if not row:
                    raise LedgerError("comparison case evidence is missing")
                selected_cases.append(json.loads(bytes(row["content"])))
            calibration_record = None
            if checked["grader_digest"] is not None:
                row = self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='grader_calibration' AND content_digest=?",
                    (project_id, checked["grader_digest"]),
                ).fetchone()
                assert row is not None
                calibration_record = json.loads(bytes(row["content"]))
            from .learning_engine import compare_candidate

            derived = compare_candidate(
                candidate=persisted_candidate,
                trials=selected_trials,
                cases=selected_cases,
                client_version=checked["client_version"],
                client_capability=capability,
                independent_review_digest=checked["independent_review_digest"],
                evaluation_assignment_digest=checked[
                    "evaluation_assignment_digest"
                ],
                grader_calibration=calibration_record,
                subjective_grading_required=calibration_record is not None,
            )
            if derived != checked:
                raise LedgerError("comparison differs from durable evaluation evidence")
        else:
            comparison = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type='comparison' "
                "AND content_digest=?",
                (project_id, checked["comparison_digest"]),
            ).fetchone()
            if not comparison:
                raise LedgerError("canary report lacks its promoting comparison")
            comparison_record = json.loads(bytes(comparison["content"]))
            if (
                comparison_record["decision"] != "promote"
                or comparison_record["candidate_digest"] != checked["candidate_digest"]
                or comparison_record["champion_digest"] != checked["champion_digest"]
            ):
                raise LedgerError("canary comparison does not match its candidate")
            case_rows = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_type='evaluation_case'",
                (project_id,),
            ).fetchall()
            canary_cases = {
                str(row["content_digest"]): json.loads(bytes(row["content"]))
                for row in case_rows
            }
            if any(
                digest not in canary_cases
                or canary_cases[digest]["corpus_state"] != "canary"
                or canary_cases[digest]["reviewed"] is not True
                or canary_cases[digest]["frozen"] is not True
                or canary_cases[digest]["task_slice"] != persisted_candidate["task_slice"]
                for digest in checked["case_digests"]
            ):
                raise LedgerError("canary cases are not frozen canary evidence")
            started_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='promotion'",
                (project_id,),
            ).fetchall()
            started = [
                {
                    **json.loads(bytes(row["content"])),
                    "_source_sequence": int(row["source_sequence"]),
                }
                for row in started_rows
                if json.loads(bytes(row["content"]))["action"] == "canary_started"
                and json.loads(bytes(row["content"]))["candidate_digest"]
                == checked["candidate_digest"]
                and json.loads(bytes(row["content"]))["canary_case_digests"]
                == checked["case_digests"]
            ]
            if not started:
                raise LedgerError("canary report has no durable canary start")
            started_sequence = max(record["_source_sequence"] for record in started)
            outcome_rows = self._connection.execute(
                "SELECT content,content_digest,source_sequence FROM records WHERE project_id=? "
                "AND record_type='outcome' AND source='evaluator'",
                (project_id,),
            ).fetchall()
            outcomes = {
                str(row["content_digest"]): {
                    **json.loads(bytes(row["content"])),
                    "_source_sequence": int(row["source_sequence"]),
                }
                for row in outcome_rows
            }
            candidate_digests: list[str] = []
            derived_deterministic = True
            derived_security = True
            derived_criteria = True
            derived_regressions = 0
            from .learning_engine import candidate_matches_outcome
            for pair in checked["case_outcomes"]:
                case_digest = pair["case_digest"]
                champion_digest = pair["champion_outcome_digest"]
                candidate_digest_value = pair["candidate_outcome_digest"]
                if champion_digest not in outcomes or candidate_digest_value not in outcomes:
                    raise LedgerError("canary outcome evidence is missing")
                champion = outcomes[champion_digest]
                outcome = outcomes[candidate_digest_value]
                candidate_digests.append(candidate_digest_value)
                if (
                    champion["_source_sequence"] >= started_sequence
                    or outcome["_source_sequence"] <= started_sequence
                    or champion["evaluation_candidate_digest"] != checked["champion_digest"]
                    or outcome["evaluation_candidate_digest"] != checked["candidate_digest"]
                    or champion["evaluation_case_digest"] != case_digest
                    or outcome["evaluation_case_digest"] != case_digest
                    or champion["task_slice"] != persisted_candidate["task_slice"]
                    or outcome["task_slice"] != persisted_candidate["task_slice"]
                    or not candidate_matches_outcome(
                        persisted_candidate, "champion", champion
                    )
                    or not candidate_matches_outcome(
                        persisted_candidate, "candidate", outcome
                    )
                ):
                    raise LedgerError("canary outcome does not prove its case")
                derived_deterministic &= outcome["deterministic_pass"]
                derived_security &= outcome["security_pass"]
                derived_criteria &= (
                    outcome["required_criteria_passed"]
                    == outcome["required_criteria_total"]
                )
                derived_regressions += (
                    outcome["quality_micros"] < champion["quality_micros"]
                )
            if (
                checked["deterministic_pass"] is not derived_deterministic
                or checked["security_pass"] is not derived_security
                or checked["required_criteria_pass"] is not derived_criteria
                or checked["quality_regressions"] != derived_regressions
            ):
                raise LedgerError("canary report hard gates do not match outcomes")
            latest_outcome_sequence = max(
                outcomes[digest]["_source_sequence"] for digest in candidate_digests
            )
            if not any(
                review["_source_sequence"] > latest_outcome_sequence
                and review["subject_digests"] == sorted(candidate_digests)
                for review in valid_reviews
            ):
                raise LedgerError("canary independent review predates or omits canary evidence")

    def _owner_invalidated_run_ids_locked(self, project_id: str) -> set[str]:
        invalidated: set[str] = set()
        with self._lock:
            rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='feedback'",
                (project_id,),
            ).fetchall()
        for row in rows:
            feedback = json.loads(bytes(row["content"]))
            if feedback["label"] in {"rejected", "corrected"}:
                invalidated.add(feedback["run_id"])
        return invalidated

    def _candidate_evidence_invalidated_locked(
        self, project_id: str, candidate: dict[str, Any]
    ) -> bool:
        invalidated = self._owner_invalidated_run_ids_locked(project_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_type='outcome'",
                (project_id,),
            ).fetchall()
        invalidated_outcomes = {
            str(row["content_digest"])
            for row in rows
            if json.loads(bytes(row["content"]))["run_id"] in invalidated
        }
        return any(
            digest in invalidated_outcomes
            for digest in candidate["evidence_digests"]
        )

    def _validate_promotion_evidence_locked(
        self, project_id: str, promotion: dict[str, Any]
    ) -> None:
        candidate_rows = self._connection.execute(
            "SELECT content FROM records WHERE project_id=? "
            "AND record_type='candidate'",
            (project_id,),
        ).fetchall()
        candidates = [
            json.loads(bytes(row["content"])) for row in candidate_rows
            if json.loads(bytes(row["content"]))["candidate_digest"]
            == promotion["candidate_digest"]
        ]
        if len(candidates) != 1 or self._candidate_evidence_invalidated_locked(
            project_id, candidates[0]
        ):
            raise LedgerError("lifecycle candidate was invalidated by owner feedback")
        action = promotion["action"]
        if action in {"canary_started", "promoted"}:
            raise LedgerError("authorized transition requires atomic grant consumption")
        evidence_type = None
        required_decision = None
        required_pass = None
        if action in {"evaluated", "rejected", "drift_detected"}:
            evidence_type = "comparison"
            required_decision = "promote" if action == "evaluated" else "reject"
        elif action in {"canary_passed", "canary_failed"}:
            evidence_type = "canary_report"
            required_pass = action == "canary_passed"
        evidence = None
        if evidence_type is not None:
            row = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type=? "
                "AND content_digest=?",
                (project_id, evidence_type, promotion["evidence_digest"]),
            ).fetchone()
            if not row:
                raise LedgerError("lifecycle transition evidence is missing")
            evidence = json.loads(bytes(row["content"]))
            if (
                evidence["candidate_digest"] != promotion["candidate_digest"]
                or evidence["champion_digest"] != promotion["champion_digest"]
                or (required_decision is not None and evidence["decision"] != required_decision)
                or (required_pass is not None and evidence["passed"] is not required_pass)
            ):
                raise LedgerError("lifecycle transition evidence does not match")
            if action.startswith("canary_") and (
                evidence["case_digests"] != promotion["canary_case_digests"]
            ):
                raise LedgerError("canary transition cases do not match evidence")
        prior_rows = self._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='promotion'",
            (project_id,),
        ).fetchall()
        prior = [json.loads(bytes(row["content"])) for row in prior_rows]
        related = [
            record for record in prior
            if record["candidate_digest"] == promotion["candidate_digest"]
            and record["champion_digest"] == promotion["champion_digest"]
        ]
        prerequisites = {
            "canary_passed": {"canary_started"},
            "canary_failed": {"canary_started"},
            "drift_detected": {"promoted"},
            "reverted": {"promoted"},
        }.get(action, set())
        if not prerequisites.issubset({record["action"] for record in related}):
            raise LedgerError("lifecycle transition prerequisite is missing")

    def _append_checked_locked(
        self,
        project_id: str,
        checked: dict[str, Any],
        *,
        producer: str,
        timestamp: int,
    ) -> dict[str, Any]:
        if checked["record_type"] == "candidate":
            self._validate_candidate_eligibility_locked(project_id, checked)
        source = SOURCE_BY_PRODUCER[producer]
        content = canonical_json(checked)
        digest = content_digest(checked)
        expires = (
            None
            if checked["record_type"]
            in {
                "evaluation_case", "qa_evidence", "evidence_review", "evaluation_trial", "candidate", "comparison", "canary_report",
                "grader_calibration", "owner_grant", "promotion", "learning_export",
                "billing_attestation",
            }
            else timestamp + DEFAULT_RETENTION_MS
        )
        existing = self._connection.execute(
            "SELECT content_digest, source_sequence FROM records "
            "WHERE project_id=? AND record_id=?",
            (project_id, checked["record_id"]),
        ).fetchone()
        if existing:
            if existing["content_digest"] != digest:
                raise LedgerError("record identifier conflicts with existing content")
            return {
                "stored": False,
                "duplicate": True,
                "source_sequence": int(existing["source_sequence"]),
            }
        row = self._connection.execute(
            "SELECT COALESCE(MAX(source_sequence),0), COALESCE(MAX(observed_at_ms),0) "
            "FROM records WHERE project_id=?",
            (project_id,),
        ).fetchone()
        sequence = int(row[0]) + 1
        late = int(checked.get("observed_at_ms", timestamp) < int(row[1]))
        self._connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                checked["record_id"],
                checked["record_type"],
                source,
                sequence,
                int(checked.get("observed_at_ms", timestamp)),
                timestamp,
                expires,
                content,
                digest,
                late,
            ),
        )
        self._auto_link_locked(project_id, checked)
        return {
            "stored": True,
            "duplicate": False,
            "source_sequence": sequence,
            "late": bool(late),
        }

    def _auto_link_locked(self, project_id: str, record: dict[str, Any]) -> None:
        """Record complete derivation lineage in the same append transaction."""

        target = record["record_id"]

        def ids_for_digest(digest: str | None) -> list[str]:
            if digest is None:
                return []
            return [
                str(row[0])
                for row in self._connection.execute(
                    "SELECT record_id FROM records WHERE project_id=? AND content_digest=?",
                    (project_id, digest),
                ).fetchall()
            ]

        def ids_for_candidate(digest: str | None) -> list[str]:
            if digest is None:
                return []
            rows = self._connection.execute(
                "SELECT record_id,content FROM records WHERE project_id=? "
                "AND record_type='candidate'",
                (project_id,),
            ).fetchall()
            return [
                str(row["record_id"])
                for row in rows
                if json.loads(bytes(row["content"]))["candidate_digest"] == digest
            ]

        def link(source_ids: Iterator[str] | list[str] | set[str], relation: str) -> None:
            for source in set(source_ids):
                if source == target:
                    continue
                self._connection.execute(
                    "INSERT OR IGNORE INTO record_links VALUES(?,?,?,?)",
                    (project_id, source, target, relation),
                )

        kind = record["record_type"]
        if kind == "outcome":
            rows = self._connection.execute(
                "SELECT record_id,content FROM records WHERE project_id=? "
                "AND record_type='event' AND source='host'",
                (project_id,),
            ).fetchall()
            link(
                [
                    str(row["record_id"])
                    for row in rows
                    if (
                        (event := json.loads(bytes(row["content"]))) ["run_id"]
                        == record["run_id"]
                        and event["assignment_id"] == record["assignment_id"]
                        and event["item_digest"] in record["evidence_digests"]
                    )
                ],
                "derived_from",
            )
        elif kind == "feedback":
            rows = self._connection.execute(
                "SELECT record_id,content FROM records WHERE project_id=? "
                "AND record_type='outcome'",
                (project_id,),
            ).fetchall()
            link(
                [
                    str(row["record_id"])
                    for row in rows
                    if json.loads(bytes(row["content"]))["run_id"] == record["run_id"]
                ],
                "corrects",
            )
        elif kind == "evaluation_case":
            link([record["source_outcome_id"]], "derived_from")
            if record["supersedes_case_id"] is not None:
                link([record["supersedes_case_id"]], "derived_from")
        elif kind == "candidate":
            link(
                [
                    item
                    for digest in record["evidence_digests"]
                    for item in ids_for_digest(digest)
                ],
                "evidence",
            )
        elif kind == "evaluation_assignment":
            link(ids_for_candidate(record["candidate_digest"]), "evaluates")
            link(record["case_ids"], "evidence")
        elif kind == "qa_evidence":
            link(ids_for_digest(record["evaluation_assignment_digest"]), "derived_from")
            link(ids_for_digest(record["case_digest"]), "evidence")
            link(ids_for_digest(record["host_outcome_digest"]), "evidence")
            link(ids_for_digest(record["verifier_host_outcome_digest"]), "evidence")
        elif kind == "evidence_review":
            link(
                [
                    item
                    for digest in record["subject_digests"]
                    for item in ids_for_digest(digest)
                ],
                "evidence",
            )
        elif kind == "evaluation_trial":
            link(ids_for_digest(record["evaluation_assignment_digest"]), "derived_from")
            link([record["case_id"]], "evidence")
            link(ids_for_digest(record["outcome_digest"]), "evidence")
        elif kind == "comparison":
            link(ids_for_candidate(record["candidate_digest"]), "evaluates")
            link(ids_for_digest(record["evaluation_assignment_digest"]), "derived_from")
            link(record["case_ids"], "evidence")
            link(
                [item for digest in record["trial_digests"] for item in ids_for_digest(digest)],
                "evidence",
            )
            link(ids_for_digest(record["independent_review_digest"]), "evidence")
            link(ids_for_digest(record["grader_digest"]), "evidence")
        elif kind == "canary_report":
            link(ids_for_candidate(record["candidate_digest"]), "evaluates")
            link(ids_for_digest(record["comparison_digest"]), "evidence")
            link(ids_for_digest(record["independent_review_digest"]), "evidence")
            for case_outcome in record["case_outcomes"]:
                link(ids_for_digest(case_outcome["case_digest"]), "evidence")
                link(ids_for_digest(case_outcome["champion_outcome_digest"]), "evidence")
                link(ids_for_digest(case_outcome["candidate_outcome_digest"]), "evidence")
        elif kind == "owner_grant":
            link(ids_for_candidate(record["candidate_digest"]), "promotes")
            link(ids_for_digest(record["review_digest"]), "evidence")
        elif kind == "promotion":
            link(ids_for_candidate(record["candidate_digest"]), "promotes")
            link(ids_for_digest(record["evidence_digest"]), "evidence")
            if record["grant_id"] is not None:
                link([record["grant_id"]], "promotes")
            for digest in record["canary_case_digests"]:
                link(ids_for_digest(digest), "evidence")
        elif kind == "learning_export":
            link(ids_for_candidate(record["candidate_digest"]), "derived_from")

    def commit_granted_promotion(
        self,
        project_id: str,
        *,
        grant: dict[str, Any],
        promotion: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Atomically persist a grant, its transition, and durable single-use receipt."""

        try:
            checked_grant = validate_learning_record(grant)
            checked_promotion = validate_learning_record(promotion)
        except ContractError as exc:
            raise LedgerError("record admission rejected") from exc
        if checked_grant["record_type"] != "owner_grant" or checked_promotion["record_type"] != "promotion":
            raise LedgerError("granted transition record types are invalid")
        expected_action = {"canary": "canary_started", "promotion": "promoted"}[
            checked_grant["grant_kind"]
        ]
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        authorization_timestamp = time.time_ns() // 1_000_000
        from .learning_engine import OwnerGrantAuthority

        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        if (
            not authority.verify(checked_grant)
            or not checked_grant["issued_at_ms"]
            <= authorization_timestamp
            < checked_grant["expires_at_ms"]
            or checked_grant["project_id"] != project_id
            or checked_promotion["grant_id"] != checked_grant["record_id"]
            or checked_promotion["candidate_digest"] != checked_grant["candidate_digest"]
            or checked_promotion["champion_digest"] != checked_grant["champion_digest"]
            or checked_promotion["action"] != expected_action
            or checked_promotion["evidence_digest"] != checked_grant["review_digest"]
            or content_digest(checked_promotion["canary_case_digests"])
            != checked_grant["bounds_digest"]
        ):
            raise LedgerError("grant and transition do not match")
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            self._validate_granted_transition_locked(
                project_id, checked_grant, checked_promotion
            )
            used = self._connection.execute(
                "SELECT promotion_record_id FROM consumed_grants "
                "WHERE grant_id=?",
                (checked_grant["record_id"],),
            ).fetchone()
            if used:
                if used["promotion_record_id"] == checked_promotion["record_id"]:
                    return {"stored": False, "duplicate": True}
                raise LedgerError("owner grant was already consumed")
            self._append_checked_locked(
                project_id, checked_grant, producer="owner", timestamp=timestamp
            )
            self._append_checked_locked(
                project_id, checked_promotion, producer="lifecycle", timestamp=timestamp
            )
            self._connection.execute(
                "INSERT INTO consumed_grants VALUES(?,?,?,?,?,?)",
                (
                    project_id,
                    checked_grant["record_id"],
                    checked_promotion["record_id"],
                    checked_grant["candidate_digest"],
                    checked_promotion["action"],
                    timestamp,
                ),
            )
        self.state.harden_database_files()
        return {"stored": True, "duplicate": False}

    def _validate_granted_transition_locked(
        self,
        project_id: str,
        grant: dict[str, Any],
        promotion: dict[str, Any],
    ) -> None:
        prerequisite = self._connection.execute(
            "SELECT ingested_at_ms FROM records WHERE project_id=? AND content_digest=? "
            "AND record_type=?",
            (
                project_id,
                grant["review_digest"],
                "comparison" if grant["grant_kind"] == "canary" else "canary_report",
            ),
        ).fetchone()
        if (
            not prerequisite
            or grant["issued_at_ms"] < int(prerequisite["ingested_at_ms"])
        ):
            raise LedgerError("owner grant predates its prerequisite evidence")
        rows = self._connection.execute(
            "SELECT record_type,content,content_digest FROM records WHERE project_id=?",
            (project_id,),
        ).fetchall()
        records = [
            (str(row["record_type"]), json.loads(bytes(row["content"])), str(row["content_digest"]))
            for row in rows
        ]
        candidates = [
            record for kind, record, _ in records
            if kind == "candidate" and record["candidate_digest"] == grant["candidate_digest"]
        ]
        if not candidates or any(
            record["base_digest"] != grant["champion_digest"] for record in candidates
        ):
            raise LedgerError("granted transition has no matching persisted candidate")
        if self._candidate_evidence_invalidated_locked(project_id, candidates[0]):
            raise LedgerError("granted candidate was invalidated by owner feedback")
        prior_promotions = [record for kind, record, _ in records if kind == "promotion"]
        if grant["grant_kind"] == "canary":
            persisted_canary_cases = {
                digest
                for kind, record, digest in records
                if kind == "evaluation_case"
                and record["corpus_state"] == "canary"
                and record["reviewed"] is True
                and record["frozen"] is True
                and record["task_slice"] == candidates[0]["task_slice"]
            }
            if not set(promotion["canary_case_digests"]).issubset(
                persisted_canary_cases
            ):
                raise LedgerError("canary grant bounds are not frozen canary cases")
            comparison = [
                record for kind, record, digest in records
                if kind == "comparison" and digest == grant["review_digest"]
            ]
            if not comparison or any(
                record["decision"] != "promote"
                or record["candidate_digest"] != grant["candidate_digest"]
                or record["champion_digest"] != grant["champion_digest"]
                for record in comparison
            ):
                raise LedgerError("canary grant lacks a promoting comparison")
            if not any(
                record["action"] == "evaluated"
                and record["candidate_digest"] == grant["candidate_digest"]
                and record["champion_digest"] == grant["champion_digest"]
                and record["evidence_digest"] == grant["review_digest"]
                for record in prior_promotions
            ):
                raise LedgerError("canary grant lacks a durable evaluated transition")
            return
        canary_reports = [
            record for kind, record, digest in records
            if kind == "canary_report" and digest == grant["review_digest"]
        ]
        if not canary_reports or any(
            record["passed"] is not True
            or record["candidate_digest"] != grant["candidate_digest"]
            or record["champion_digest"] != grant["champion_digest"]
            or content_digest(record["case_digests"]) != grant["bounds_digest"]
            for record in canary_reports
        ):
            raise LedgerError("promotion grant lacks a successful canary report")
        matching_actions = [
            record for record in prior_promotions
            if record["candidate_digest"] == grant["candidate_digest"]
            and record["champion_digest"] == grant["champion_digest"]
            and record["canary_case_digests"] == promotion["canary_case_digests"]
        ]
        canary_started = any(record["action"] == "canary_started" for record in matching_actions)
        canary_passed = any(
            record["action"] == "canary_passed"
            and record["evidence_digest"] == grant["review_digest"]
            for record in matching_actions
        )
        if not canary_started or not canary_passed:
            raise LedgerError("promotion grant lacks durable canary transitions")

    def link(self, project_id: str, from_id: str, to_id: str, relation: str) -> None:
        if relation not in {"evidence", "derived_from", "corrects", "evaluates", "promotes"}:
            raise LedgerError("record relation is unsupported")
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT OR IGNORE INTO record_links VALUES(?,?,?,?)",
                    (project_id, from_id, to_id, relation),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("record link is invalid") from exc

    def write_sanitized_record_file(
        self,
        project_id: str,
        *,
        file_id: str,
        kind: str,
        record: dict[str, Any],
        expires_at_ms: int | None = None,
    ) -> Path:
        """Write a validated metadata-only record; callers cannot register arbitrary bytes."""

        if kind not in {"artifact", "export"}:
            raise LedgerError("sanitized record file kind is unsupported")
        try:
            checked = validate_learning_record(record)
        except ContractError as exc:
            raise LedgerError("stored record admission rejected") from exc
        with self._lock:
            persisted = self._connection.execute(
                "SELECT content_digest FROM records WHERE project_id=? AND record_id=?",
                (project_id, checked["record_id"]),
            ).fetchone()
            if not persisted or persisted["content_digest"] != content_digest(checked):
                raise LedgerError("stored record must already exist in its project partition")
            path = self._write_owner_file(kind, project_id, file_id, canonical_json(checked))
            try:
                self._register_stored_file(
                    project_id,
                    file_id=file_id,
                    kind=kind,
                    path=path,
                    digest=hashlib.sha256(canonical_json(checked)).hexdigest(),
                    source_record_id=checked["record_id"],
                    expires_at_ms=expires_at_ms,
                )
            except Exception:
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
            return path

    def create_sanitized_backup(
        self,
        project_id: str,
        *,
        file_id: str,
        expires_at_ms: int,
    ) -> Path:
        """Create a recoverable project-partition backup with a sealed manifest."""

        if expires_at_ms <= time.time_ns() // 1_000_000:
            raise LedgerError("sanitized backup expiry must be in the future")
        with self._lock:
            body = _snapshot_project_partition(
                self._connection,
                project_id,
                ledger_schema_version=LEDGER_SCHEMA_VERSION,
            )
            body_bytes = canonical_json(body)
            envelope = {
                "backup_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "body": body,
            }
            payload = canonical_json(envelope)
            if len(payload) > 128 * 1024 * 1024:
                raise LedgerError("sanitized backup is oversized")
            path = self._write_owner_file("backup", project_id, file_id, payload)
            try:
                self._register_stored_file(
                    project_id,
                    file_id=file_id,
                    kind="backup",
                    path=path,
                    digest=hashlib.sha256(payload).hexdigest(),
                    expires_at_ms=expires_at_ms,
                )
            except Exception:
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
            return path

    def verify_sanitized_backup(
        self, project_id: str, *, file_id: str
    ) -> dict[str, Any]:
        """Restore one registered backup into an isolated in-memory verifier."""

        row = self._connection.execute(
            "SELECT relative_path,content_digest FROM stored_files "
            "WHERE project_id=? AND file_id=? AND kind='backup'",
            (project_id, file_id),
        ).fetchone()
        if row is None:
            raise LedgerError("project backup is unavailable")
        path = self.state.root / str(row["relative_path"])
        _assert_no_symlink_components(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != str(row["content_digest"]):
            raise LedgerError("project backup file digest disagrees with its registry")
        body, counts, manifest_digest = _verify_backup_envelope(
            raw, expected_project_id=project_id
        )
        return {
            "verified": True,
            "backup_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_sha256": manifest_digest,
            "ledger_schema_version": body["ledger_schema_version"],
            "counts": counts,
            "isolated_restore_integrity": "ok",
            "isolated_restore_foreign_keys": "ok",
        }

    def _write_owner_file(
        self, kind: str, project_id: str, file_id: str, payload: bytes
    ) -> Path:
        if not file_id or len(file_id) > 160 or not file_id.replace("_", "").isalnum():
            raise LedgerError("stored file identifier is invalid")
        root = {
            "artifact": self.state.artifacts,
            "export": self.state.exports,
            "backup": self.state.backups,
        }[kind]
        partition = root / project_id
        partition.mkdir(mode=0o700, exist_ok=True)
        _assert_no_symlink_components(partition)
        _check_owner_mode(partition, directory=True)
        path = partition / f"{file_id}.json"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return path

    def _register_stored_file(
        self,
        project_id: str,
        *,
        file_id: str,
        kind: str,
        path: Path,
        digest: str,
        source_record_id: str | None = None,
        expires_at_ms: int | None = None,
    ) -> None:
        """Register a file created by a typed writer in this ledger."""

        if kind not in {"artifact", "export", "backup"}:
            raise LedgerError("stored file kind is unsupported")
        if not file_id or len(file_id) > 160 or not file_id.replace("_", "").isalnum():
            raise LedgerError("stored file identifier is invalid")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise LedgerError("stored file digest is invalid")
        if expires_at_ms is not None and (
            not isinstance(expires_at_ms, int)
            or isinstance(expires_at_ms, bool)
            or expires_at_ms < 0
        ):
            raise LedgerError("stored file expiry is invalid")
        root_by_kind = {
            "artifact": self.state.artifacts,
            "export": self.state.exports,
            "backup": self.state.backups,
        }
        partition = root_by_kind[kind] / project_id
        _assert_no_symlink_components(path)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(partition.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise LedgerError("stored file is outside its project partition") from exc
        if resolved.is_symlink() or not resolved.is_file():
            raise LedgerError("stored file must be a regular file")
        try:
            _check_owner_mode(resolved, directory=False)
        except StateError as exc:
            raise LedgerError("stored file permissions are unsafe") from exc
        hasher = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65_536), b""):
                    hasher.update(chunk)
        except OSError as exc:
            raise LedgerError("stored file cannot be read") from exc
        if not secrets.compare_digest(hasher.hexdigest(), digest):
            raise LedgerError("stored file digest does not match")
        relative = resolved.relative_to(self.state.root).as_posix()
        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            try:
                self._connection.execute(
                    "INSERT INTO stored_files VALUES(?,?,?,?,?,?,?)",
                    (
                        project_id,
                        file_id,
                        kind,
                        relative,
                        digest,
                        source_record_id,
                        expires_at_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("stored file registration is invalid") from exc

    @contextlib.contextmanager
    def repository(self, project_id: str) -> Iterator["ProjectRepository"]:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not row or row["status"] != "active":
                raise LedgerError("project binding is not active")
            self._active_readers += 1
        try:
            yield ProjectRepository(self, project_id)
        finally:
            with self._lock:
                self._active_readers -= 1

    def lifecycle_records(
        self, project_id: str, candidate_digest_value: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return one candidate and its ordered transitions for the lifecycle controller."""

        with self._lock:
            candidate_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type='candidate'",
                (project_id,),
            ).fetchall()
            candidates = [
                json.loads(bytes(row["content"]))
                for row in candidate_rows
                if json.loads(bytes(row["content"]))["candidate_digest"]
                == candidate_digest_value
            ]
            if len(candidates) != 1:
                raise LedgerError("candidate lifecycle record was not found")
            promotion_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type='promotion' "
                "ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
        promotions = [
            json.loads(bytes(row["content"]))
            for row in promotion_rows
            if json.loads(bytes(row["content"]))["candidate_digest"]
            == candidate_digest_value
        ]
        return candidates[0], promotions

    def routing_candidates(
        self,
        project_id: str,
        selection_slice: str,
        *,
        released_candidate_digests: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Return promoted candidates activated by the versioned release registry."""

        from .learning_engine import CandidateLifecycle, OwnerGrantAuthority

        if not released_candidate_digests:
            return []

        with self._lock:
            candidate_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='candidate' ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
            promotion_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='promotion' ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
        promotions = [
            json.loads(bytes(row["content"])) for row in promotion_rows
        ]
        active: list[tuple[int, dict[str, Any]]] = []
        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        for row in candidate_rows:
            candidate = json.loads(bytes(row["content"]))
            parts = candidate["task_slice"].split("-")
            candidate_selection_slice = (
                "-".join((parts[0], parts[1], *parts[3:]))
                if len(parts) == 7
                else None
            )
            if (
                candidate_selection_slice != selection_slice
                or candidate["scope"] != "general"
            ):
                continue
            if self._candidate_evidence_invalidated_locked(project_id, candidate):
                continue
            related = [
                record
                for record in promotions
                if record["candidate_digest"] == candidate["candidate_digest"]
            ]
            lifecycle = CandidateLifecycle.restore(
                candidate=candidate,
                champion_digest=candidate["base_digest"],
                project_id=project_id,
                authority=authority,
                digest_key=self.state.digest_key(),
                promotions=related,
            )
            selected = lifecycle.resolve_digest("")
            if (
                lifecycle.status == "promoted"
                and selected == candidate["candidate_digest"]
                and selected in released_candidate_digests
            ):
                activation = next(
                    int(promotion_rows[index]["source_sequence"])
                    for index, record in enumerate(promotions)
                    if record["candidate_digest"] == selected
                    and record["action"] in {"canary_started", "promoted"}
                )
                active.append((activation, candidate))
        if len(active) > 1:
            raise LedgerError(
                "model selection candidate is ambiguous across risk variants"
            )
        return [candidate for _, candidate in sorted(active, key=lambda item: item[0])]

    def canary_routing_candidate(
        self, project_id: str, case_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve one reviewed canary case without accepting caller-supplied digests."""

        from .learning_engine import CandidateLifecycle, OwnerGrantAuthority

        with self._lock:
            case_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_case' AND record_id=?",
                (project_id, case_id),
            ).fetchall()
            candidate_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='candidate' ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
            promotion_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='promotion' ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
            outcome_rows = self._connection.execute(
                "SELECT content,source_sequence FROM records WHERE project_id=? "
                "AND record_type='outcome' ORDER BY source_sequence",
                (project_id,),
            ).fetchall()
            attested_qa_digests = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT r.content_digest FROM records r "
                    "JOIN owner_attestations a ON a.project_id=r.project_id "
                    "AND a.subject_record_id=r.record_id "
                    "WHERE r.project_id=? AND r.record_type='qa_evidence'",
                    (project_id,),
                ).fetchall()
            }
        if len(case_rows) != 1:
            raise LedgerError("canary case was not found")
        case = json.loads(bytes(case_rows[0]["content"]))
        if (
            case["corpus_state"] != "canary"
            or not case["reviewed"]
            or not case["frozen"]
        ):
            raise LedgerError("canary case is not reviewed and frozen")
        case_digest = content_digest(case)
        promotions = [json.loads(bytes(row["content"])) for row in promotion_rows]
        outcomes = [
            (json.loads(bytes(row["content"])), int(row["source_sequence"]))
            for row in outcome_rows
        ]
        authority = OwnerGrantAuthority(self.state.owner_allowed_signers())
        active: list[dict[str, Any]] = []
        for row in candidate_rows:
            candidate = json.loads(bytes(row["content"]))
            if (
                candidate["scope"] != "general"
                or candidate["task_slice"] != case["task_slice"]
            ):
                continue
            if self._candidate_evidence_invalidated_locked(project_id, candidate):
                continue
            lifecycle = CandidateLifecycle.restore(
                candidate=candidate,
                champion_digest=candidate["base_digest"],
                project_id=project_id,
                authority=authority,
                digest_key=self.state.digest_key(),
                promotions=[
                    record for record in promotions
                    if record["candidate_digest"] == candidate["candidate_digest"]
                ],
            )
            canary_started_sequences = [
                int(promotion_rows[index]["source_sequence"])
                for index, record in enumerate(promotions)
                if record["candidate_digest"] == candidate["candidate_digest"]
                and record["action"] == "canary_started"
                and case_digest in record["canary_case_digests"]
            ]
            champion = candidate["champion_configuration"]
            configuration_fields = set(champion)
            champion_replayed = bool(canary_started_sequences) and any(
                sequence < min(canary_started_sequences)
                and outcome["status"] == "accepted"
                and outcome["evaluation_candidate_digest"] == candidate["base_digest"]
                and outcome["evaluation_case_digest"] == case_digest
                and {
                    field: outcome[field] for field in configuration_fields
                } == champion
                and bool(
                    set(outcome["evidence_digests"]) & attested_qa_digests
                )
                for outcome, sequence in outcomes
            )
            if (
                lifecycle.status == "canary_approved"
                and lifecycle.resolve_digest(case_digest)
                == candidate["candidate_digest"]
                and champion_replayed
            ):
                active.append(candidate)
        if len(active) != 1:
            raise LedgerError("canary case does not resolve to exactly one candidate")
        return case, active[0]

    def record_by_digest(
        self, project_id: str, digest: str, record_type: str
    ) -> dict[str, Any]:
        """Return one typed controller record by its canonical content digest."""

        if record_type not in {
            "comparison", "canary_report", "outcome", "grader_calibration",
            "evaluation_assignment", "evaluation_trial",
        }:
            raise LedgerError("controller record type is unsupported")
        with self._lock:
            rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type=? "
                "AND content_digest=?",
                (project_id, record_type, digest),
            ).fetchall()
        if len(rows) != 1:
            raise LedgerError("controller evidence record was not found")
        return json.loads(bytes(rows[0]["content"]))

    def trusted_routing_evidence_refs(
        self, project_id: str, task: dict[str, Any]
    ) -> frozenset[str]:
        """Resolve task-bound, QA-accepted reconnaissance outcomes."""

        checked = validate_task(task)
        references = checked["routing_evidence_refs"]
        if not references:
            return frozenset()
        expected_task_id = keyed_digest(self.state.digest_key(), checked["id"])
        accepted: set[str] = set()
        with self._lock:
            invalidated_run_ids = self._owner_invalidated_run_ids_locked(project_id)
            for reference in references:
                match = re.fullmatch(r"outcome:([0-9a-f]{64})", reference)
                if match is None:
                    raise LedgerError(
                        "routing evidence must be a typed host outcome digest"
                    )
                digest = match.group(1)
                rows = self._connection.execute(
                    "SELECT r.content,q.accepted FROM records AS r "
                    "JOIN operational_qa AS q ON q.project_id=r.project_id "
                    "AND q.host_outcome_digest=r.content_digest "
                    "WHERE r.project_id=? AND r.record_type='outcome' "
                    "AND r.source='host' AND r.content_digest=?",
                    (project_id, digest),
                ).fetchall()
                if len(rows) != 1 or int(rows[0]["accepted"]) != 1:
                    raise LedgerError("routing evidence is not QA accepted")
                outcome = json.loads(bytes(rows[0]["content"]))
                if (
                    outcome.get("execution_state", "completed") != "completed"
                    or outcome.get("semantic_verdict", "pass")
                    not in {"pass", "ready", "ready-with-corrections"}
                ):
                    raise LedgerError(
                        "routing evidence is not positive completed reconnaissance"
                    )
                if outcome["run_id"] in invalidated_run_ids:
                    raise LedgerError(
                        "routing evidence was invalidated by owner feedback"
                    )
                parts = outcome["task_slice"].split("-")
                binding = self._connection.execute(
                    "SELECT task_id_digest FROM host_run_bindings "
                    "WHERE project_id=? AND run_id=?",
                    (project_id, outcome["run_id"]),
                ).fetchall()
                if (
                    len(parts) != 7
                    or parts[0] != checked["family"]
                    or not (parts[4] == "unknown" or parts[5] == "unknown")
                    or len(binding) != 1
                    or binding[0]["task_id_digest"] != expected_task_id
                ):
                    raise LedgerError(
                        "routing evidence is not task-bound reconnaissance"
                    )
                accepted.add(reference)
        return frozenset(accepted)

    def run_learning_cycle(
        self, project_id: str, *, as_of_ms: int, max_age_ms: int = DEFAULT_RETENTION_MS
    ) -> dict[str, Any]:
        """Run bounded deterministic aggregation, case mining, and inert proposal generation."""

        from .learning_contracts import candidate_base_digest
        from .learning_engine import analyze_outcomes, generate_candidate, mine_evaluation_case

        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT status FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if not project or project["status"] != "active":
                raise LedgerError("project binding is not active")
            outcome_rows = self._connection.execute(
                "SELECT content,content_digest FROM records WHERE project_id=? "
                "AND record_type='outcome' ORDER BY source_sequence LIMIT 10001",
                (project_id,),
            ).fetchall()
            if len(outcome_rows) > 10_000:
                raise LedgerError("learning cycle evidence exceeds its bounded window")
            outcome_pairs = [
                (json.loads(bytes(row["content"])), row) for row in outcome_rows
            ]
            verified_run_ids = {
                outcome["run_id"]
                for outcome, _ in outcome_pairs
                if outcome["status"] != "unverified"
            }
            outcome_pairs = [
                (outcome, row) for outcome, row in outcome_pairs
                if not (
                    outcome["status"] == "unverified"
                    and outcome["run_id"] in verified_run_ids
                )
            ]
            outcomes = [outcome for outcome, _ in outcome_pairs]
            owner_invalidated_run_ids = self._owner_invalidated_run_ids_locked(
                project_id
            )
            existing_case_sources = {
                json.loads(bytes(row[0]))["source_outcome_id"]
                for row in self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='evaluation_case'",
                    (project_id,),
                ).fetchall()
            }
            mined = []
            for outcome in outcomes:
                if (
                    outcome["record_id"] in existing_case_sources
                    or outcome["evaluation_candidate_digest"] is not None
                    or outcome["run_id"] in owner_invalidated_run_ids
                    or outcome["status"] == "unverified"
                ):
                    continue
                criteria = {
                    "deterministic_pass": outcome["deterministic_pass"],
                    "security_pass": outcome["security_pass"],
                    "required_criteria_total": outcome["required_criteria_total"],
                    "required_criteria_passed": outcome["required_criteria_passed"],
                }
                record = mine_evaluation_case(
                    digest_key=self.state.digest_key(),
                    outcome=outcome,
                    fixture_digest=outcome["fixture_digest"],
                    criteria_digest=content_digest(criteria),
                    lineage_digest=keyed_digest(
                        self.state.digest_key(), outcome["run_id"]
                    ),
                )
                self._validate_evidence_links_locked(project_id, record)
                self._append_checked_locked(
                    project_id, record, producer="evaluator", timestamp=as_of_ms
                )
                mined.append(record["record_id"])

            analysis = analyze_outcomes(
                outcomes,
                as_of_ms=as_of_ms,
                max_age_ms=max_age_ms,
                owner_invalidated_run_ids=owner_invalidated_run_ids,
            )
            controlled_rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? "
                "AND record_type='evaluation_case' LIMIT 20001",
                (project_id,),
            ).fetchall()
            if len(controlled_rows) > 20_000:
                raise LedgerError("learning case corpus exceeds its bounded window")
            all_cases = [json.loads(bytes(row["content"])) for row in controlled_rows]
            protected_lineages = {
                case["lineage_digest"]
                for case in all_cases
                if case["corpus_state"] != "development"
            }
            protected_similarities = {
                case["similarity_group_digest"]
                for case in all_cases
                if case["corpus_state"] != "development"
                and case["similarity_group_digest"] is not None
            }
            protected_case_ids = {
                identifier
                for case in all_cases
                if case["corpus_state"] != "development"
                for identifier in (case["record_id"], case["supersedes_case_id"])
                if identifier is not None
            }
            changed = True
            while changed:
                changed = False
                for case in all_cases:
                    if (
                        case["lineage_digest"] in protected_lineages
                        or case["similarity_group_digest"] in protected_similarities
                        or case["record_id"] in protected_case_ids
                        or case["supersedes_case_id"] in protected_case_ids
                    ):
                        before = len(protected_case_ids)
                        protected_lineages.add(case["lineage_digest"])
                        if case["similarity_group_digest"] is not None:
                            protected_similarities.add(case["similarity_group_digest"])
                        protected_case_ids.add(case["record_id"])
                        if case["supersedes_case_id"] is not None:
                            protected_case_ids.add(case["supersedes_case_id"])
                        changed = changed or len(protected_case_ids) != before
            controlled_source_ids = {
                case["source_outcome_id"]
                for case in all_cases
                if (
                    case["lineage_digest"] in protected_lineages
                    or case["similarity_group_digest"] in protected_similarities
                    or case["record_id"] in protected_case_ids
                )
            }
            holdout_counts: dict[str, int] = {}
            for case in all_cases:
                if (
                    case["corpus_state"] == "promotion_holdout"
                    and case["reviewed"] is True
                    and case["frozen"] is True
                ):
                    holdout_counts[case["task_slice"]] = (
                        holdout_counts.get(case["task_slice"], 0) + 1
                    )
            existing_candidates = {
                json.loads(bytes(row[0]))["candidate_digest"]
                for row in self._connection.execute(
                    "SELECT content FROM records WHERE project_id=? "
                    "AND record_type='candidate'",
                    (project_id,),
                ).fetchall()
            }
            groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
            retry_parent = {
                str(outcome["assignment_id"]): outcome.get("retry_of_assignment_id")
                for outcome, _ in outcome_pairs
            }

            def retry_root(outcome: dict[str, Any]) -> str:
                current = str(outcome["assignment_id"])
                seen: set[str] = set()
                while retry_parent.get(current) is not None and current not in seen:
                    seen.add(current)
                    current = str(retry_parent[current])
                return current

            grouped_lineages: dict[tuple[str, str], set[str]] = {}
            for outcome, row in outcome_pairs:
                if (
                    outcome["evaluation_candidate_digest"] is not None
                    or outcome["record_id"] in controlled_source_ids
                    or outcome["accepted"] is not True
                    or outcome.get("semantic_verdict", "pass")
                    not in {"pass", "ready", "ready-with-corrections"}
                    or outcome["run_id"] in owner_invalidated_run_ids
                ):
                    continue
                execution = {
                    key: outcome[key]
                    for key in (
                        "model", "effort", "role", "topology", "max_parallel",
                        "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                        "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                        "client_digest",
                    )
                }
                group_key = (outcome["task_slice"], content_digest(execution))
                lineage = retry_root(outcome)
                if lineage in grouped_lineages.setdefault(group_key, set()):
                    continue
                grouped_lineages[group_key].add(lineage)
                groups.setdefault(group_key, []).append(
                    (outcome, str(row["content_digest"]))
                )
            proposed = []
            proposal_status = "configuration_limit" if len(groups) > 64 else "complete"
            groups_to_compare = {} if len(groups) > 64 else groups
            for (task_slice, _), champion_records in sorted(groups_to_compare.items()):
                if (
                    holdout_counts.get(task_slice, 0) < 20
                    or len(champion_records) < 20
                ):
                    continue
                champion = champion_records[0][0]
                champion_config = {
                    key: champion[key]
                    for key in (
                        "model", "effort", "role", "topology", "max_parallel",
                        "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                        "policy_digest", "agent_digest", "skill_digests", "prompt_digest",
                        "client_digest",
                    )
                }
                for (other_slice, _), candidate_records in sorted(groups_to_compare.items()):
                    if other_slice != task_slice or candidate_records is champion_records:
                        continue
                    if len(candidate_records) < 20:
                        continue
                    champion_by_fixture = {
                        item[0]["fixture_digest"]: item for item in champion_records
                    }
                    candidate_by_fixture = {
                        item[0]["fixture_digest"]: item for item in candidate_records
                    }
                    matched_fixtures = sorted(
                        champion_by_fixture.keys() & candidate_by_fixture.keys()
                    )
                    if len(matched_fixtures) < 20:
                        continue
                    # Candidate records carry at most 64 evidence digests. Retain
                    # one deterministic observation per configuration for each of
                    # the most recent 32 matched fixtures.
                    matched_fixtures = matched_fixtures[-32:]
                    selected_champion_records = [
                        champion_by_fixture[fixture] for fixture in matched_fixtures
                    ]
                    selected_candidate_records = [
                        candidate_by_fixture[fixture] for fixture in matched_fixtures
                    ]
                    challenger = selected_candidate_records[0][0]
                    challenger_config = {
                        key: challenger[key] for key in champion_config
                    }
                    unchanged = set(champion_config) - {"model", "effort"}
                    if any(champion_config[key] != challenger_config[key] for key in unchanged):
                        continue
                    champion_outcomes = [item[0] for item in selected_champion_records]
                    challenger_outcomes = [item[0] for item in selected_candidate_records]
                    both = champion_outcomes + challenger_outcomes
                    if not all(
                        item["coverage"] == "end_to_end_complete"
                        and item["deterministic_pass"]
                        and item["security_pass"]
                        and item["required_criteria_passed"]
                        == item["required_criteria_total"]
                        and item["usage"]["input_tokens"] is not None
                        and item["usage"]["output_tokens"] is not None
                        and item["usage"]["model_calls"] is not None
                        for item in both
                    ):
                        continue

                    def mean(records: list[dict[str, Any]], field: str) -> float:
                        if field == "tokens":
                            return sum(
                                item["usage"]["input_tokens"]
                                + item["usage"]["output_tokens"]
                                for item in records
                            ) / len(records)
                        if field == "model_calls":
                            return sum(item["usage"][field] for item in records) / len(records)
                        return sum(item[field] for item in records) / len(records)

                    champion_tokens = mean(champion_outcomes, "tokens")
                    challenger_tokens = mean(challenger_outcomes, "tokens")
                    champion_calls = mean(champion_outcomes, "model_calls")
                    challenger_calls = mean(challenger_outcomes, "model_calls")
                    if (
                        mean(challenger_outcomes, "quality_micros")
                        < mean(champion_outcomes, "quality_micros")
                        or challenger_tokens > champion_tokens
                        or challenger_calls > champion_calls
                        or not (
                            challenger_tokens <= champion_tokens * 0.9
                            or challenger_calls <= champion_calls * 0.9
                        )
                    ):
                        continue
                    before = {
                        "model": champion_config["model"],
                        "effort": champion_config["effort"],
                    }
                    after = {
                        "model": challenger_config["model"],
                        "effort": challenger_config["effort"],
                    }
                    if before == after:
                        continue
                    record = generate_candidate(
                        digest_key=self.state.digest_key(),
                        scope="project",
                        task_slice=task_slice,
                        target="model_effort",
                        effect="optimize",
                        base_digest=candidate_base_digest(champion_config),
                        champion_configuration=champion_config,
                        change_manifest=[
                            {
                                "path_code": "routing.model_effort",
                                "before_digest": content_digest(before),
                                "after_digest": content_digest(after),
                                "before_value": before,
                                "after_value": after,
                            }
                        ],
                        evidence_digests=sorted(
                            item[1]
                            for item in selected_champion_records
                            + selected_candidate_records
                        ),
                        eligibility_receipt={
                            "task_slice": task_slice,
                            "evidence_set_digest": content_digest(
                                sorted(
                                    item[1]
                                    for item in selected_champion_records
                                    + selected_candidate_records
                                )
                            ),
                            "independent_case_count": len(matched_fixtures),
                            "usage_complete": True,
                            "version_unconfounded": True,
                            "task_mix_matched": True,
                            "retry_deduplicated": True,
                            "verifier_evidence_complete": True,
                        },
                    )
                    if record["candidate_digest"] in existing_candidates:
                        continue
                    try:
                        self._validate_evidence_links_locked(project_id, record)
                        self._append_checked_locked(
                            project_id, record, producer="evaluator", timestamp=as_of_ms
                        )
                    except LedgerError:
                        # Automatic proposal generation is opportunistic. Invalid or
                        # incomplete observations remain durable learning evidence but
                        # cannot make the whole maintenance cycle fail.
                        continue
                    existing_candidates.add(record["candidate_digest"])
                    proposed.append(record["candidate_digest"])
        self.state.harden_database_files()
        return {
            "mined_case_ids": mined,
            "proposed_candidate_digests": proposed,
            "analysis": analysis,
            "proposal_status": proposal_status,
        }

    def evaluation_case(self, project_id: str, record_id: str) -> dict[str, Any]:
        """Return one exact controlled case for the evaluation controller only."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=? "
                "AND record_type='evaluation_case'",
                (project_id, record_id),
            ).fetchall()
        if len(rows) != 1:
            raise LedgerError("evaluation case was not found")
        record = json.loads(bytes(rows[0]["content"]))
        if record["corpus_state"] == "development":
            raise LedgerError("evaluation case is not in a controlled corpus")
        return record

    def _records_for_project(self, project_id: str, record_type: str | None) -> list[dict[str, Any]]:
        query = "SELECT content, source_sequence, late FROM records WHERE project_id=?"
        params: tuple[Any, ...] = (project_id,)
        if record_type is not None:
            query += " AND record_type=?"
            params += (record_type,)
        query += " ORDER BY source_sequence"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [
            {
                **json.loads(bytes(row["content"])),
                "_source_sequence": int(row["source_sequence"]),
                "_late": bool(row["late"]),
            }
            for row in rows
        ]

    def health_increment(self, project_id: str, code: str, *, now_ms: int | None = None) -> None:
        if code not in {"admission_rejected", "queue_full", "collector_error", "conflict"}:
            raise LedgerError("health code is unsupported")
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO collector_health VALUES(?,?,1,?) "
                "ON CONFLICT(project_id,code) DO UPDATE SET count=count+1, updated_at_ms=excluded.updated_at_ms",
                (project_id, code, timestamp),
            )

    def health_snapshot(self, project_id: str) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT code,count,updated_at_ms FROM collector_health "
                "WHERE project_id=? ORDER BY code",
                (project_id,),
            ).fetchall()
        return {
            str(row["code"]): {
                "count": int(row["count"]),
                "updated_at_ms": int(row["updated_at_ms"]),
            }
            for row in rows
        }

    def ingestion_snapshot(
        self,
        project_id: str,
        *,
        client_failures: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Return exact-project hook ingestion counts without record content or IDs."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT event_code,accepted_count,last_accepted_at_ms "
                "FROM hook_ingestion WHERE project_id=? ORDER BY event_code",
                (project_id,),
            ).fetchall()
            host_row = self._connection.execute(
                "SELECT COUNT(*) AS opened,"
                "SUM(consumed_at_ms IS NOT NULL) AS terminal,"
                "SUM(binding_state='pending') AS pending,"
                "SUM(native_assignment_id IS NOT NULL) AS native_bound,"
                "SUM(usage_complete=1) AS usage_complete,"
                "SUM(usage_complete=0) AS usage_unknown,"
                "SUM(binding_state='abandoned') AS abandoned "
                "FROM host_run_bindings WHERE project_id=?",
                (project_id,),
            ).fetchone()
            orchestration_rows = self._connection.execute(
                "SELECT status,COUNT(*) AS count FROM orchestrations "
                "WHERE project_id=? GROUP BY status",
                (project_id,),
            ).fetchall()
            qa_rows = self._connection.execute(
                "SELECT accepted,COUNT(*) AS count FROM operational_qa "
                "WHERE project_id=? GROUP BY accepted",
                (project_id,),
            ).fetchall()
        counts = {code: 0 for code in HOOK_EVENT_CODES}
        last_accepted: int | None = None
        for row in rows:
            event_code = str(row["event_code"])
            if event_code in counts:
                counts[event_code] = int(row["accepted_count"])
            accepted_at = int(row["last_accepted_at_ms"])
            last_accepted = (
                accepted_at if last_accepted is None else max(last_accepted, accepted_at)
            )
        failures = client_failures or {}
        rejected_count = 0
        last_rejected: int | None = None
        for code in HOOK_REJECTION_CODES:
            counter = failures.get(code)
            if not isinstance(counter, dict):
                continue
            rejected_count += int(counter.get("count", 0))
            updated = counter.get("updated_at_ms")
            if isinstance(updated, int) and not isinstance(updated, bool):
                last_rejected = updated if last_rejected is None else max(last_rejected, updated)
        snapshot = empty_ingestion_snapshot(registered=True)
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        age = None if last_accepted is None else max(0, timestamp - last_accepted)
        freshness = (
            "awaiting_hook_review_or_first_event"
            if age is None
            else "fresh" if age <= INGESTION_FRESH_MS else "stale"
        )
        snapshot.update(
            accepted_hook_event_count=sum(counts.values()),
            last_accepted_hook_event_at_ms=last_accepted,
            rejected_hook_event_count=rejected_count,
            last_rejected_hook_event_at_ms=last_rejected,
            freshness={"state": freshness, "age_ms": age},
            hook_event_counts=counts,
            managed_lifecycle={
                "orchestrations_active": sum(
                    int(row["count"])
                    for row in orchestration_rows if row["status"] == "active"
                ),
                "orchestrations_closed": sum(
                    int(row["count"])
                    for row in orchestration_rows
                    if row["status"] not in {"active", "paused"}
                ),
                "orchestrations_paused": sum(
                    int(row["count"])
                    for row in orchestration_rows if row["status"] == "paused"
                ),
                "host_runs_opened": int(host_row["opened"] or 0),
                "host_runs_terminal": int(host_row["terminal"] or 0),
                "host_runs_pending": int(host_row["pending"] or 0),
                "host_runs_abandoned": int(host_row["abandoned"] or 0),
                "native_agent_bindings": int(host_row["native_bound"] or 0),
                "operational_qa_passed": sum(
                    int(row["count"]) for row in qa_rows if bool(row["accepted"])
                ),
                "operational_qa_failed": sum(
                    int(row["count"]) for row in qa_rows if not bool(row["accepted"])
                ),
                "host_usage_complete": int(host_row["usage_complete"] or 0),
                "host_usage_unknown": int(host_row["usage_unknown"] or 0),
            },
        )
        return snapshot

    def expire(self, project_id: str, *, now_ms: int) -> int:
        with self._lock:
            if self._active_readers:
                raise LedgerError("expiry requires closed scoped readers")
            links = self._connection.execute(
                "SELECT from_record_id,to_record_id FROM record_links WHERE project_id=?",
                (project_id,),
            ).fetchall()
            undirected: dict[str, set[str]] = {}
            for row in links:
                left, right = str(row[0]), str(row[1])
                undirected.setdefault(left, set()).add(right)
                undirected.setdefault(right, set()).add(left)
            promoted_rows = self._connection.execute(
                "SELECT record_id,content FROM records WHERE project_id=? "
                "AND record_type='promotion'",
                (project_id,),
            ).fetchall()
            protected = {
                str(row["record_id"])
                for row in promoted_rows
                if json.loads(bytes(row["content"]))["action"] == "promoted"
            }
            protected_frontier = list(protected)
            while protected_frontier:
                current = protected_frontier.pop()
                for linked in undirected.get(current, set()):
                    if linked not in protected:
                        protected.add(linked)
                        protected_frontier.append(linked)
            rows = self._connection.execute(
                "SELECT record_id FROM records WHERE project_id=? AND expires_at_ms IS NOT NULL "
                "AND expires_at_ms<=?",
                (project_id, now_ms),
            ).fetchall()
            identifiers = {str(row[0]) for row in rows} - protected
            frontier = list(identifiers)
            adjacency: dict[str, set[str]] = {}
            for row in links:
                left, right = str(row[0]), str(row[1])
                adjacency.setdefault(left, set()).add(right)
            while frontier:
                current = frontier.pop()
                for linked in adjacency.get(current, set()):
                    if linked not in identifiers and linked not in protected:
                        identifiers.add(linked)
                        frontier.append(linked)
            stored = self._connection.execute(
                "SELECT relative_path,kind,source_record_id,expires_at_ms "
                "FROM stored_files WHERE project_id=?",
                (project_id,),
            ).fetchall()
            file_rows = [
                row
                for row in stored
                if (
                    (row["expires_at_ms"] is not None and int(row["expires_at_ms"]) <= now_ms)
                    or (row["source_record_id"] in identifiers)
                    or (bool(identifiers) and row["kind"] == "backup")
                )
            ]
            if not identifiers and not file_rows:
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM repair_receipts WHERE project_id=? AND expires_at_ms<=?",
                        (project_id, now_ms),
                    )
                    self._connection.execute(
                        "DELETE FROM operational_check_receipts "
                        "WHERE project_id=? AND expires_at_ms<=?",
                        (project_id, now_ms),
                    )
                return 0
            self._begin_exclusive_maintenance()
            try:
                self._connection.execute(
                    "DELETE FROM repair_receipts WHERE project_id=? AND expires_at_ms<=?",
                    (project_id, now_ms),
                )
                self._connection.execute(
                    "DELETE FROM operational_check_receipts "
                    "WHERE project_id=? AND expires_at_ms<=?",
                    (project_id, now_ms),
                )
                for row in file_rows:
                    self._connection.execute(
                        "DELETE FROM stored_files WHERE project_id=? AND relative_path=?",
                        (project_id, str(row["relative_path"])),
                    )
                for record_id in identifiers:
                    self._connection.execute(
                        "DELETE FROM records WHERE project_id=? AND record_id=?",
                        (project_id, record_id),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            finally:
                self._finish_exclusive_maintenance()
            for row in file_rows:
                self._remove_registered_path(project_id, str(row["relative_path"]))
            for root in (self.state.artifacts, self.state.exports, self.state.backups):
                with contextlib.suppress(OSError):
                    (root / project_id).rmdir()
        return len(identifiers)

    def delete_project(self, project_id: str, *, now_ms: int | None = None) -> str:
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        deletion_id = f"del_{secrets.token_hex(16)}"
        with self._lock:
            if self._active_readers:
                raise LedgerError("project deletion requires closed scoped readers")
            rows = self._connection.execute(
                "SELECT relative_path FROM stored_files WHERE project_id=?", (project_id,)
            ).fetchall()
            self._begin_exclusive_maintenance()
            try:
                self._connection.execute("DELETE FROM hook_ingestion WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM records WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM sessions WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM run_provenance WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM stored_files WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM collector_health WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM repair_receipts WHERE project_id=?", (project_id,))
                self._connection.execute("DELETE FROM host_run_bindings WHERE project_id=?", (project_id,))
                updated = self._connection.execute(
                    "UPDATE projects SET canonical_root=NULL,root_digest=NULL,status='deleted',deleted_at_ms=? "
                    "WHERE project_id=? AND status='active'",
                    (timestamp, project_id),
                ).rowcount
                if not updated:
                    raise LedgerError("project binding is not active")
                self._connection.execute(
                    "INSERT INTO tombstones VALUES(?,?,?)",
                    (project_id, deletion_id, timestamp),
                )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            finally:
                self._finish_exclusive_maintenance()
            for row in rows:
                self._remove_registered_path(project_id, str(row[0]))
            for root in (self.state.artifacts, self.state.exports, self.state.backups):
                with contextlib.suppress(OSError):
                    (root / project_id).rmdir()
        return deletion_id

    def _begin_exclusive_maintenance(self) -> None:
        """Acquire a rollback-journal exclusive transaction or fail before deletion."""

        self._connection.execute("PRAGMA busy_timeout = 50")
        try:
            checkpoint = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise LedgerError("maintenance requires all external readers to close")
            mode = str(self._connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0])
            if mode.lower() != "delete":
                raise LedgerError("maintenance could not acquire rollback journal mode")
            self._connection.execute("BEGIN EXCLUSIVE")
        except (sqlite3.Error, LedgerError) as exc:
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("maintenance requires all external readers to close") from exc

    def _finish_exclusive_maintenance(self) -> None:
        try:
            self._connection.execute("VACUUM")
            mode_cursor = self._connection.execute("PRAGMA journal_mode = WAL")
            mode_row = mode_cursor.fetchone()
            mode_cursor.close()
            mode = str(mode_row[0])
            if mode.lower() != "wal":
                raise LedgerError("ledger journal mode could not be restored")
            # VACUUM has already committed a complete rollback-journal image.
            # Re-entering WAL is sufficient here; a same-connection checkpoint can
            # report SQLITE_LOCKED while schema statements remain cached even though
            # no external reader exists.
        except sqlite3.Error as exc:
            raise LedgerError("ledger maintenance could not be finalized") from exc
        finally:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self.state.harden_database_files()

    def _remove_registered_path(self, project_id: str, relative: str) -> None:
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 3
            or path.parts[0] not in {"artifacts", "exports", "backups"}
            or path.parts[1] != project_id
        ):
            raise LedgerError("registered file path is unsafe")
        descriptor = os.open(
            self.state.root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in path.parent.parts:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            try:
                os.unlink(path.name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        except OSError as exc:
            raise LedgerError("registered file path is unsafe") from exc
        finally:
            os.close(descriptor)

    def storage_residuals(self) -> list[str]:
        residuals = []
        for suffix in ("-wal", "-journal"):
            path = Path(str(self.state.database) + suffix)
            if path.exists() and path.stat().st_size:
                residuals.append(path.name)
        return residuals


class ProjectRepository:
    """Fixed project-scoped read surface."""

    def __init__(self, ledger: LearningLedger, project_id: str) -> None:
        self.__ledger = ledger
        self.project_id = project_id

    def records(self, record_type: str | None = None) -> list[dict[str, Any]]:
        if record_type is not None and record_type not in RECORD_TYPES:
            raise LedgerError("record type is unsupported")
        records = self.__ledger._records_for_project(self.project_id, record_type)
        all_cases = self.__ledger._records_for_project(
            self.project_id, "evaluation_case"
        )
        hidden_development = {
            case["supersedes_case_id"]
            for case in all_cases
            if case["corpus_state"] != "development"
        }
        return [
            record
            for record in records
            if record.get("record_type") != "evaluation_case"
            or (
                record.get("corpus_state") == "development"
                and record.get("record_id") not in hidden_development
            )
        ]

    def record(self, record_id: str, record_type: str | None = None) -> dict[str, Any]:
        """Return one immutable record from this project partition."""

        records = self.records(record_type)
        matches = [record for record in records if record["record_id"] == record_id]
        if len(matches) != 1:
            raise LedgerError("project record was not found")
        return {key: value for key, value in matches[0].items() if not key.startswith("_")}


class CollectorServer:
    """Sequential AF_UNIX collector. Only this loop calls the ledger writer."""

    def __init__(
        self,
        ledger: LearningLedger,
        *,
        client_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.ledger = ledger
        self.state = ledger.state
        self._bindings: dict[str, SessionBinding] = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._startup_error: OSError | None = None
        self._started_at_ms: int | None = None
        self._clients = client_capabilities or load_client_capabilities(
            Path(__file__).resolve().parent.parent / "config" / "learning-clients.json"
        )

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX") or fcntl is None:
            raise CollectorError("local Unix sockets are unavailable")
        if self._thread and self._thread.is_alive():
            raise CollectorError("collector is already running")
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        with self.ledger._lock:
            if self.ledger._collector_active:
                raise CollectorError("another collector owns this state root")
            self.ledger._collector_active = True
        self._thread = threading.Thread(target=self.serve, name="codex-agents-collector")
        self._thread.start()
        if not self._ready.wait(5):
            self._release_lock()
            raise CollectorError("collector did not start")
        if self._startup_error is not None:
            self._release_lock()
            raise CollectorError("collector endpoint could not be created") from self._startup_error

    def serve(self) -> None:
        if self.state.socket.exists():
            self.state.socket.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = listener
        try:
            listener.bind(str(self.state.socket))
        except OSError as exc:
            self._startup_error = exc
            self._ready.set()
            listener.close()
            self._socket = None
            return
        os.chmod(self.state.socket, 0o600)
        listener.listen(16)
        listener.settimeout(0.1)
        self._started_at_ms = time.time_ns() // 1_000_000
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(1)
                    response = self._handle(connection)
                    connection.sendall(canonical_json(response) + b"\n")
        finally:
            listener.close()
            self._socket = None
            with contextlib.suppress(FileNotFoundError):
                self.state.socket.unlink()

    def _handle(self, connection: socket.socket) -> dict[str, Any]:
        raw = bytearray()
        while b"\n" not in raw:
            chunk = connection.recv(8192)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_REQUEST_BYTES:
                return {"ok": False, "code": "request_oversized"}
        try:
            request = parse_bounded_json(
                bytes(raw).split(b"\n", 1)[0], max_bytes=MAX_REQUEST_BYTES
            )
        except ContractError:
            return {"ok": False, "code": "request_invalid"}
        if not isinstance(request, dict) or request.get("protocol") != COLLECTOR_PROTOCOL_VERSION:
            return {"ok": False, "code": "protocol_mismatch"}
        try:
            if request.get("op") == "bind" and set(request) == {
                "protocol",
                "op",
                "project_root",
                "session_id",
                "producer",
                "client_version",
            }:
                now_ms = time.time_ns() // 1_000_000
                self._bindings = {
                    key: value
                    for key, value in self._bindings.items()
                    if value.expires_at_ms > now_ms
                }
                if len(self._bindings) >= 4096:
                    return {"ok": False, "code": "binding_capacity"}
                client = self._clients.get(request["client_version"])
                if client is None or request["client_version"].startswith(
                    "codex-eval-fixture-"
                ):
                    return {"ok": False, "code": "client_unvalidated"}
                producer = request["producer"]
                if producer not in client.get("producers", []):
                    return {"ok": False, "code": "producer_rejected"}
                if producer == "hook":
                    coverage = client["hook_coverage"]
                    event_codes = frozenset(value[0] for value in HOOK_EVENTS.values())
                elif producer == "app_server":
                    coverage = client["app_server_coverage"]
                    event_codes = frozenset(
                        {
                            "turn_started", "turn_completed", "turn_failed",
                            "turn_interrupted", "item_completed", "model_rerouted",
                            "usage_snapshot",
                        }
                    )
                else:
                    return {"ok": False, "code": "operation_rejected"}
                project_id, session_digest, root_run_id = self.ledger.bind_session(
                    Path(request["project_root"]),
                    request["session_id"],
                    request["producer"],
                    request["client_version"],
                )
                token = f"bnd_{secrets.token_hex(24)}"
                self._bindings[token] = SessionBinding(
                    token,
                    project_id,
                    session_digest,
                    request["producer"],
                    root_run_id,
                    coverage,
                    event_codes,
                    now_ms + 300_000,
                )
                return {"ok": True, "binding_token": token}
            if request.get("op") == "ingest" and set(request) == {
                "protocol",
                "op",
                "binding_token",
                "record",
            }:
                binding = self._bindings.pop(request["binding_token"], None)
                if not binding or binding.expires_at_ms <= time.time_ns() // 1_000_000:
                    return {"ok": False, "code": "binding_invalid"}
                record = validate_learning_record(request["record"])
                if record["record_type"] != "event" or not (
                    record["run_id"] == binding.root_run_id
                    or record["parent_run_id"] == binding.root_run_id
                ):
                    return {"ok": False, "code": "provenance_mismatch"}
                if (
                    record["coverage"] != binding.coverage
                    or record["event_code"] not in binding.event_codes
                ):
                    return {"ok": False, "code": "capability_mismatch"}
                result = self.ledger.append(
                    binding.project_id, record, producer=binding.producer
                )
                return {"ok": True, **result}
            if request.get("op") == "issue_result_repair" and set(request) == {
                "protocol",
                "op",
                "project_root",
                "assignment_digest",
                "reason_code",
            }:
                project_id = self.ledger.project_for_root(Path(request["project_root"]))
                receipt_id = self.ledger.issue_result_repair_receipt(
                    project_id,
                    request["assignment_digest"],
                    request["reason_code"],
                )
                return {"ok": True, "receipt_id": receipt_id}
            if request.get("op") == "health" and set(request) in (
                {"protocol", "op"},
                {"protocol", "op", "project_root"},
            ):
                response: dict[str, Any] = {
                    "ok": True,
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "runtime_version": PACKAGE_VERSION,
                    "runtime_digest": COLLECTOR_RUNTIME_DIGEST,
                    "started_at_ms": self._started_at_ms,
                }
                if "project_root" in request:
                    project_root = Path(request["project_root"])
                    project_id = self.ledger.project_for_root(project_root)
                    response["collector_health"] = self.ledger.health_snapshot(
                        project_id
                    )
                    response["ingestion"] = self.ledger.ingestion_snapshot(
                        project_id,
                        client_failures=client_health_snapshot(
                            self.ledger.state, project_root
                        ),
                    )
                return response
            if request.get("op") == "shutdown" and set(request) == {"protocol", "op"}:
                self._stop.set()
                return {"ok": True}
            return {"ok": False, "code": "operation_invalid"}
        except ProjectNotRegisteredError:
            return {"ok": False, "code": "project_unregistered"}
        except (LedgerError, StateError, TypeError, ValueError):
            return {"ok": False, "code": "operation_rejected"}

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
            if self._thread.is_alive():
                raise CollectorError("collector did not stop")
        self._release_lock()

    def _release_lock(self) -> None:
        with self.ledger._lock:
            self.ledger._collector_active = False

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the collector thread; return true when it has stopped."""

        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()


class CollectorClient:
    """Bounded, fail-fast client used by hook and App Server adapters."""

    def __init__(self, socket_path: Path, *, timeout: float = 0.25) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = canonical_json(request) + b"\n"
        if len(payload) > MAX_REQUEST_BYTES:
            raise CollectorError("collector request is oversized")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(str(self.socket_path))
            client.sendall(payload)
            raw = bytearray()
            while b"\n" not in raw:
                chunk = client.recv(8192)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 16_384:
                    raise CollectorError("collector response is oversized")
        except OSError as exc:
            raise CollectorError("collector is unavailable") from exc
        finally:
            client.close()
        try:
            response = json.loads(bytes(raw).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectorError("collector response is invalid") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            code = response.get("code", "unknown") if isinstance(response, dict) else "unknown"
            raise CollectorError(f"collector rejected request: {code}", code=code)
        return response

    def bind(
        self, project_root: Path, session_id: str, producer: str, client_version: str
    ) -> str:
        response = self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "bind",
                "project_root": str(project_root.resolve()),
                "session_id": session_id,
                "producer": producer,
                "client_version": client_version,
            }
        )
        return str(response["binding_token"])

    def ingest(self, binding_token: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "ingest",
                "binding_token": binding_token,
                "record": record,
            }
        )

    def health(self, project_root: Path | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "protocol": COLLECTOR_PROTOCOL_VERSION,
            "op": "health",
        }
        if project_root is not None:
            request["project_root"] = str(project_root.resolve())
        response = self._request(request)
        server_schema = response.get("schema_version")
        server_version = _version_tuple(response.get("runtime_version"))
        client_version = _version_tuple(PACKAGE_VERSION)
        if client_version is None:  # guarded when PACKAGE_VERSION is loaded
            raise CollectorError("installed package version is invalid")
        if (
            server_version is not None
            and server_version > client_version
        ) or (
            isinstance(server_schema, int)
            and not isinstance(server_schema, bool)
            and server_schema > LEDGER_SCHEMA_VERSION
        ):
            raise CollectorNewerVersionError(
                "a newer collector owns the shared learning state; update this project",
                code="runtime_newer",
            )
        if (
            not isinstance(server_schema, int)
            or isinstance(server_schema, bool)
            or server_schema < LEDGER_SCHEMA_VERSION
            or server_version is None
            or server_version < client_version
        ):
            raise CollectorUpgradeRequired(
                "collector runtime is older than the installed package",
                code="runtime_upgrade_required",
            )
        if response.get("runtime_digest") != COLLECTOR_RUNTIME_DIGEST:
            raise CollectorBuildConflictError(
                "collector build conflicts with this package version",
                code="runtime_conflict",
            )
        return response

    def shutdown(self) -> None:
        self._request(
            {"protocol": COLLECTOR_PROTOCOL_VERSION, "op": "shutdown"}
        )

    def issue_result_repair(
        self,
        project_root: Path,
        assignment_digest: str,
        reason_code: str,
    ) -> str:
        response = self._request(
            {
                "protocol": COLLECTOR_PROTOCOL_VERSION,
                "op": "issue_result_repair",
                "project_root": str(project_root.resolve()),
                "assignment_digest": assignment_digest,
                "reason_code": reason_code,
            }
        )
        return str(response["receipt_id"])


CLIENT_HEALTH_CODES = {
    "collector_start_failed",
    "collector_unavailable",
    "maintenance_active",
    "hook_normalization_rejected",
    "hook_binding_rejected",
    "hook_ingest_rejected",
}


def _client_health_data(state: LearningState) -> dict[str, Any]:
    if not state.client_health.exists():
        return {"schema_version": 1, "projects": {}}
    if state.client_health.is_symlink():
        raise StateError("client health path is unsafe")
    _check_owner_mode(state.client_health, directory=False)
    try:
        raw = state.client_health.read_bytes()
        if len(raw) > 65_536:
            raise StateError("client health state is invalid")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("client health state is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("projects"), dict)
    ):
        raise StateError("client health state is invalid")
    allowed_codes = CLIENT_HEALTH_CODES
    for project_digest, counters in value["projects"].items():
        if (
            not isinstance(project_digest, str)
            or len(project_digest) != 64
            or any(character not in "0123456789abcdef" for character in project_digest)
            or not isinstance(counters, dict)
            or set(counters) - allowed_codes
        ):
            raise StateError("client health state is invalid")
        for counter in counters.values():
            if (
                not isinstance(counter, dict)
                or set(counter) != {"count", "updated_at_ms"}
                or not isinstance(counter["count"], int)
                or isinstance(counter["count"], bool)
                or counter["count"] < 1
                or not isinstance(counter["updated_at_ms"], int)
                or isinstance(counter["updated_at_ms"], bool)
                or counter["updated_at_ms"] < 0
            ):
                raise StateError("client health state is invalid")
    return value


def client_health_increment(
    state: LearningState,
    project_root: Path,
    code: str,
    *,
    now_ms: int | None = None,
) -> None:
    """Persist one content-free, project-keyed client failure counter."""

    if code not in CLIENT_HEALTH_CODES or fcntl is None:
        return
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    descriptor = os.open(
        state.client_health_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _client_health_data(state)
        projects = data["projects"]
        if project_digest not in projects and len(projects) >= 4096:
            return
        project = projects.setdefault(project_digest, {})
        current = project.get(code, {"count": 0, "updated_at_ms": 0})
        timestamp = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        project[code] = {
            "count": int(current.get("count", 0)) + 1,
            "updated_at_ms": timestamp,
        }
        temporary = state.root / f".collector-client-health.{secrets.token_hex(8)}.tmp"
        payload = canonical_json(data) + b"\n"
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state.client_health)
            os.chmod(state.client_health, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def client_health_snapshot(state: LearningState, project_root: Path) -> dict[str, Any]:
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    return dict(_client_health_data(state)["projects"].get(project_digest, {}))


def client_health_clear(state: LearningState, project_root: Path) -> None:
    """Remove one deleted project's content-free client counters."""

    if fcntl is None or not state.client_health.exists():
        return
    root = _canonical_project_root(project_root)
    project_digest = keyed_digest(state.digest_key(), str(root))
    descriptor = os.open(
        state.client_health_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _client_health_data(state)
        if data["projects"].pop(project_digest, None) is None:
            return
        temporary = state.root / f".collector-client-health.{secrets.token_hex(8)}.tmp"
        descriptor_out = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor_out, "wb") as handle:
                handle.write(canonical_json(data) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state.client_health)
            os.chmod(state.client_health, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_collector(
    state: LearningState,
    cli_path: Path,
    *,
    project_root: Path,
    timeout: float = 1.0,
) -> dict[str, Any]:
    """Ensure one detached collector is healthy without storing its project path."""

    require_authorized_collector_runtime(state)
    client = CollectorClient(state.socket)
    try:
        return client.health()
    except CollectorVersionError:
        client_health_increment(state, project_root, "collector_start_failed")
        raise
    except CollectorError:
        pass
    if fcntl is None:
        client_health_increment(state, project_root, "collector_start_failed")
        raise CollectorError("collector auto-start is unavailable")
    descriptor = os.open(
        state.runtime_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            client_health_increment(state, project_root, "maintenance_active")
            raise CollectorError("collector maintenance is active") from exc
        return _start_collector_locked(
            state,
            cli_path,
            project_root=project_root,
            timeout=timeout,
        )
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _start_collector_locked(
    state: LearningState,
    cli_path: Path,
    *,
    project_root: Path,
    timeout: float = 1.0,
) -> dict[str, Any]:
    """Start the collector while the caller owns the runtime maintenance lock."""

    require_authorized_collector_runtime(state)
    client = CollectorClient(state.socket)
    try:
        return client.health()
    except CollectorVersionError:
        client_health_increment(state, project_root, "collector_start_failed")
        raise
    except CollectorError:
        pass
    subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(cli_path.resolve(strict=True)),
            "learning-serve",
            "--state-root",
            str(state.root),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        close_fds=True,
        env={"PATH": os.defpath},
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client.health()
        except CollectorError:
            time.sleep(0.02)
    client_health_increment(state, project_root, "collector_start_failed")
    raise CollectorError("collector did not become healthy")


def _wait_for_writer_release(state: LearningState, deadline: float) -> bool:
    """Wait until the previous collector has released the ledger writer lock."""

    if fcntl is None:
        return True
    descriptor = os.open(
        state.lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)
    finally:
        os.close(descriptor)


def _wait_for_collector_shutdown(
    client: CollectorClient,
    state: LearningState,
    deadline: float,
) -> bool:
    while time.monotonic() < deadline:
        try:
            client.health()
        except CollectorVersionError:
            time.sleep(0.02)
            continue
        except CollectorError:
            return _wait_for_writer_release(state, deadline)
        time.sleep(0.02)
    return False


class CollectorMaintenance:
    """Temporarily quiesce an active collector for one direct ledger command."""

    def __init__(
        self,
        state: LearningState,
        cli_path: Path,
        project_root: Path,
        descriptor: int | None,
        restart: bool,
    ) -> None:
        self.state = state
        self.cli_path = cli_path
        self.project_root = project_root
        self.descriptor = descriptor
        self.restart = restart

    @classmethod
    def acquire(
        cls,
        state: LearningState,
        cli_path: Path,
        project_root: Path,
        *,
        timeout: float = 2.0,
        owner_registration: bool = False,
    ) -> "CollectorMaintenance":
        if not owner_registration:
            require_authorized_collector_runtime(state)
        if fcntl is None:
            return cls(state, cli_path, project_root, None, False)
        descriptor = os.open(
            state.runtime_lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise CollectorError("collector maintenance lock is busy") from exc
                time.sleep(0.02)
        transferred = False
        try:
            client = CollectorClient(state.socket)
            try:
                client.health()
            except CollectorUpgradeRequired:
                if not owner_registration:
                    raise
            except CollectorVersionError:
                raise
            except CollectorError:
                if _wait_for_writer_release(state, deadline):
                    transferred = True
                    return cls(state, cli_path, project_root, descriptor, False)
                raise CollectorError("collector writer did not stop for maintenance")
            client.shutdown()
            if _wait_for_collector_shutdown(client, state, deadline):
                transferred = True
                return cls(state, cli_path, project_root, descriptor, True)
            raise CollectorError("collector did not stop for maintenance")
        finally:
            if not transferred:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def resume(self) -> None:
        descriptor = self.descriptor
        self.descriptor = None
        try:
            if self.restart:
                _start_collector_locked(
                    self.state,
                    self.cli_path,
                    project_root=self.project_root,
                )
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
