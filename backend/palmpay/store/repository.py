"""SQLite-backed repository.

SQLite is the prototype choice: zero setup, and the schema is ordinary SQL that
ports to Postgres unchanged. The one thing worth carrying forward verbatim is
the shard index -- looking up candidates by ``shard`` is what keeps
identification at 1:small-N instead of a full table scan as enrollment grows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import AuditEvent, ConsentRecord, CustomerProfile, ProfileStatus, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    customer_id       TEXT PRIMARY KEY,
    shard             TEXT NOT NULL,
    engine_id         TEXT NOT NULL,
    wrapped_dek       BLOB,
    enc_template      BLOB,
    enc_payment_token BLOB,
    enc_pii           BLOB,
    hint_type         TEXT NOT NULL DEFAULT 'public',
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- The index that makes 1:small-N identification possible.
CREATE INDEX IF NOT EXISTS idx_profiles_shard
    ON profiles (shard, status);

CREATE TABLE IF NOT EXISTS consents (
    consent_id      TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    purposes        TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    withdrawn_at    TEXT,
    evidence_digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consents_customer
    ON consents (customer_id);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    customer_id TEXT,
    merchant_id TEXT,
    outcome     TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created
    ON audit_log (created_at);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Repository:
    """Data access for profiles, consent and audit.

    A single connection guarded by a lock. Adequate for the prototype; a
    production deployment swaps in a connection pool against Postgres, which is
    why all SQL here stays standard.
    """

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.database_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- profiles -------------------------------------------------------------

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> CustomerProfile:
        return CustomerProfile(
            customer_id=row["customer_id"],
            shard=row["shard"],
            engine_id=row["engine_id"],
            wrapped_dek=bytes(row["wrapped_dek"] or b""),
            enc_template=bytes(row["enc_template"] or b""),
            enc_payment_token=bytes(row["enc_payment_token"] or b""),
            enc_pii=bytes(row["enc_pii"] or b""),
            hint_type=row["hint_type"],
            status=ProfileStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or utc_now(),
        )

    def create_profile(self, profile: CustomerProfile) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO profiles (
                    customer_id, shard, engine_id, wrapped_dek, enc_template,
                    enc_payment_token, enc_pii, hint_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.customer_id,
                    profile.shard,
                    profile.engine_id,
                    profile.wrapped_dek,
                    profile.enc_template,
                    profile.enc_payment_token,
                    profile.enc_pii,
                    profile.hint_type,
                    profile.status.value,
                    _iso(profile.created_at),
                    _iso(profile.updated_at),
                ),
            )
            self._conn.commit()

    def get_profile(self, customer_id: str) -> CustomerProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM profiles WHERE customer_id = ?", (customer_id,)
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def find_candidates(self, shard: str, limit: int) -> list[CustomerProfile]:
        """Active profiles in one shard: the candidate set for identification.

        ``limit`` is a safety valve, not a paging device. If a shard is at the
        limit the hint has stopped narrowing effectively and the caller should
        treat that as an accuracy problem, not silently match the first N.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM profiles
                WHERE shard = ? AND status = ?
                ORDER BY created_at
                LIMIT ?
                """,
                (shard, ProfileStatus.ACTIVE.value, limit),
            ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def count_in_shard(self, shard: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM profiles WHERE shard = ? AND status = ?",
                (shard, ProfileStatus.ACTIVE.value),
            ).fetchone()
        return int(row["n"])

    def count_profiles(self, status: ProfileStatus | None = ProfileStatus.ACTIVE) -> int:
        with self._lock:
            if status is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM profiles WHERE status = ?", (status.value,)
                ).fetchone()
        return int(row["n"])

    def update_payment_token(self, customer_id: str, enc_payment_token: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE profiles SET enc_payment_token = ?, updated_at = ? WHERE customer_id = ?",
                (enc_payment_token, _iso(utc_now()), customer_id),
            )
            self._conn.commit()

    def crypto_shred(self, customer_id: str) -> bool:
        """Erase a customer by destroying their key material.

        Drops the wrapped DEK *and* overwrites the ciphertext columns. The
        overwrite is belt-and-braces: destroying the DEK alone already makes
        the ciphertext meaningless, including in any backup that has already
        been taken, which is the whole point. Clearing the live rows just
        avoids retaining bytes there is no longer any purpose in holding.

        The row itself is kept as a tombstone so the customer id is never
        reissued and so audit and consent records keep a valid referent.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE profiles
                SET wrapped_dek = NULL,
                    enc_template = NULL,
                    enc_payment_token = NULL,
                    enc_pii = NULL,
                    status = ?,
                    updated_at = ?
                WHERE customer_id = ? AND status = ?
                """,
                (
                    ProfileStatus.SHREDDED.value,
                    _iso(utc_now()),
                    customer_id,
                    ProfileStatus.ACTIVE.value,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # -- consent --------------------------------------------------------------

    def record_consent(self, consent: ConsentRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO consents (
                    consent_id, customer_id, purposes, policy_version,
                    granted_at, withdrawn_at, evidence_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consent.consent_id,
                    consent.customer_id,
                    json.dumps(list(consent.purposes)),
                    consent.policy_version,
                    _iso(consent.granted_at),
                    _iso(consent.withdrawn_at) if consent.withdrawn_at else None,
                    consent.evidence_digest,
                ),
            )
            self._conn.commit()

    def withdraw_consent(self, customer_id: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE consents SET withdrawn_at = ? WHERE customer_id = ? AND withdrawn_at IS NULL",
                (_iso(utc_now()), customer_id),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_consents(self, customer_id: str) -> list[ConsentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consents WHERE customer_id = ? ORDER BY granted_at",
                (customer_id,),
            ).fetchall()
        return [
            ConsentRecord(
                consent_id=row["consent_id"],
                customer_id=row["customer_id"],
                purposes=tuple(json.loads(row["purposes"])),
                policy_version=row["policy_version"],
                granted_at=_parse_dt(row["granted_at"]) or utc_now(),
                withdrawn_at=_parse_dt(row["withdrawn_at"]),
                evidence_digest=row["evidence_digest"],
            )
            for row in rows
        ]

    # -- audit ----------------------------------------------------------------

    def append_audit(
        self,
        event_type: str,
        outcome: str,
        customer_id: str | None = None,
        merchant_id: str | None = None,
        detail: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            customer_id=customer_id,
            merchant_id=merchant_id,
            outcome=outcome,
            detail=detail or {},
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log (
                    event_id, event_type, customer_id, merchant_id,
                    outcome, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.customer_id,
                    event.merchant_id,
                    event.outcome,
                    json.dumps(event.detail, separators=(",", ":")),
                    _iso(event.created_at),
                ),
            )
            self._conn.commit()
        return event

    def recent_audit(self, limit: int = 50) -> list[AuditEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            AuditEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                customer_id=row["customer_id"],
                merchant_id=row["merchant_id"],
                outcome=row["outcome"],
                detail=json.loads(row["detail"]),
                created_at=_parse_dt(row["created_at"]) or utc_now(),
            )
            for row in rows
        ]
