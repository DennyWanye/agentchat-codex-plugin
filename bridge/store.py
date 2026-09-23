from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class StoredDelivery:
    delivery_id: str
    message_id: str
    lease_token: str
    payload: dict[str, Any]
    status: str
    outcome: dict[str, Any] | None


class CredentialStore:
    """0600 JSON credential file with atomic replacement."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        try:
            with self.path.open(encoding="utf-8") as fh:
                value = json.load(fh)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError("credentials file must contain an object")
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".credentials.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def revoke_local(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


class InboxStore:
    """Durable local inbox and idempotency ledger backed by SQLite."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
              delivery_id TEXT PRIMARY KEY,
              message_id TEXT NOT NULL UNIQUE,
              lease_token TEXT NOT NULL DEFAULT '',
              payload TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','acked','failed')),
              outcome TEXT,
              received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              handled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS deliveries_status_idx ON deliveries(status);
            """
        )
        columns = {str(row["name"]) for row in self._db.execute("PRAGMA table_info(deliveries)").fetchall()}
        if "lease_token" not in columns:
            self._db.execute("ALTER TABLE deliveries ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''")
        self._db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._db.close()

    def put(self, delivery_id: str, message_id: str, lease_token: str, payload: dict[str, Any]) -> StoredDelivery:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self._db.execute(
            "INSERT OR IGNORE INTO deliveries(delivery_id,message_id,lease_token,payload,status) VALUES(?,?,?,?,'pending')",
            (delivery_id, message_id, lease_token, encoded),
        )
        self._db.execute(
            "UPDATE deliveries SET lease_token=?, payload=? WHERE delivery_id=? AND status='pending'",
            (lease_token, encoded, delivery_id),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM deliveries WHERE message_id=?", (message_id,)).fetchone()
        assert row is not None
        return self._row(row)

    def get(self, delivery_id: str) -> StoredDelivery | None:
        row = self._db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, *, limit: int = 100) -> list[StoredDelivery]:
        limit = max(1, min(int(limit), 1000))
        rows = self._db.execute("SELECT * FROM deliveries ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mark(self, delivery_id: str, status: str, outcome: dict[str, Any] | None = None) -> None:
        if status not in {"pending", "acked", "failed"}:
            raise ValueError("invalid delivery status")
        self._db.execute(
            "UPDATE deliveries SET status=?, outcome=?, handled_at=CURRENT_TIMESTAMP WHERE delivery_id=?",
            (status, json.dumps(outcome, ensure_ascii=False, sort_keys=True) if outcome is not None else None, delivery_id),
        )
        self._db.commit()

    def counts(self) -> dict[str, int]:
        rows = self._db.execute("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status").fetchall()
        result = {"pending": 0, "acked": 0, "failed": 0}
        result.update({str(row["status"]): int(row["n"]) for row in rows})
        return result

    def _row(self, row: sqlite3.Row) -> StoredDelivery:
        return StoredDelivery(
            delivery_id=str(row["delivery_id"]),
            message_id=str(row["message_id"]),
            lease_token=str(row["lease_token"]),
            payload=json.loads(row["payload"]),
            status=str(row["status"]),
            outcome=json.loads(row["outcome"]) if row["outcome"] else None,
        )
