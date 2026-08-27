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

from .models import (
    AuditEvent,
    ConsentRecord,
    CustomerCredential,
    PalmTemplate,
    CustomerProfile,
    ProfileStatus,
    utc_now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    customer_id       TEXT PRIMARY KEY,
    shard             TEXT NOT NULL,
    engine_id         TEXT NOT NULL,
    wrapped_dek       BLOB,
    enc_payment_token BLOB,
    enc_pii           BLOB,
    hint_type         TEXT NOT NULL DEFAULT 'public',
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- PD-21: one row per enrolled hand. A customer with an injured hand should
-- still be able to pay, which means more than one template per customer.
CREATE TABLE IF NOT EXISTS palm_templates (
    template_id  TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    engine_id    TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT 'primary',
    enc_template BLOB NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_palm_templates_customer
    ON palm_templates (customer_id);

-- The index that makes 1:small-N identification possible.
CREATE INDEX IF NOT EXISTS idx_profiles_shard
    ON profiles (shard, status);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    device_label  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_credentials_customer
    ON credentials (customer_id, revoked_at);

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

-- PD-16: our own record of every approved charge. The gateway knows the
-- transaction but not who is entitled to refund it, so without this a refund
-- endpoint could not tell an authorised merchant from any other caller.
CREATE TABLE IF NOT EXISTS payments (
    transaction_id TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL,
    merchant_id    TEXT NOT NULL,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    refunded_minor INTEGER NOT NULL DEFAULT 0,
    voided         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_merchant
    ON payments (merchant_id, created_at);

-- PD-08: PSD2 low-value exemption counters. Durable and shared, because an
-- in-memory counter resets on restart and is not seen by the next till, which
-- makes the allowance trivially resettable by walking to another terminal.
CREATE TABLE IF NOT EXISTS low_value_usage (
    customer_id      TEXT PRIMARY KEY,
    cumulative_minor INTEGER NOT NULL DEFAULT 0,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

-- PD-07: fixed-window rate limiting. Durable for the same reason: a limiter
-- that forgets on restart is not a limiter.
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       TEXT NOT NULL,
    window_start TEXT NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

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
                    customer_id, shard, engine_id, wrapped_dek,
                    enc_payment_token, enc_pii, hint_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.customer_id,
                    profile.shard,
                    profile.engine_id,
                    profile.wrapped_dek,
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

        Erasable from any state except already-shredded. Matching only ACTIVE
        would refuse a customer who paused first (PD-22) and then asked for
        deletion -- which is the most likely order for someone leaving.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE profiles
                SET wrapped_dek = NULL,
                    enc_payment_token = NULL,
                    enc_pii = NULL,
                    status = ?,
                    updated_at = ?
                WHERE customer_id = ? AND status != ?
                """,
                (
                    ProfileStatus.SHREDDED.value,
                    _iso(utc_now()),
                    customer_id,
                    ProfileStatus.SHREDDED.value,
                ),
            )
            if cursor.rowcount:
                # Destroying the DEK already renders these unreadable; dropping
                # the rows avoids retaining bytes there is no purpose in
                # holding.
                self._conn.execute(
                    "DELETE FROM palm_templates WHERE customer_id = ?", (customer_id,)
                )
            self._conn.commit()
            return cursor.rowcount > 0

    # -- palm templates (PD-21) -----------------------------------------------

    def add_template(self, template: PalmTemplate) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO palm_templates (
                    template_id, customer_id, engine_id, label,
                    enc_template, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    template.template_id,
                    template.customer_id,
                    template.engine_id,
                    template.label,
                    template.enc_template,
                    _iso(template.created_at),
                ),
            )
            self._conn.commit()

    def templates_for(self, customer_id: str) -> list[PalmTemplate]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM palm_templates WHERE customer_id = ? ORDER BY created_at",
                (customer_id,),
            ).fetchall()
        return [
            PalmTemplate(
                template_id=row["template_id"],
                customer_id=row["customer_id"],
                engine_id=row["engine_id"],
                enc_template=bytes(row["enc_template"]),
                label=row["label"],
                created_at=_parse_dt(row["created_at"]) or utc_now(),
            )
            for row in rows
        ]

    def delete_templates(self, customer_id: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM palm_templates WHERE customer_id = ?", (customer_id,)
            )
            self._conn.commit()
            return cursor.rowcount

    def set_profile_status(self, customer_id: str, status: ProfileStatus) -> bool:
        """Move a profile between active and suspended.

        Refuses to touch a shredded profile: there is no key left, so
        reactivating one would produce an account that exists but can never be
        read.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE profiles SET status = ?, updated_at = ? "
                "WHERE customer_id = ? AND status != ?",
                (status.value, _iso(utc_now()), customer_id, ProfileStatus.SHREDDED.value),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def iter_profiles(self, status: ProfileStatus | None = None) -> list[CustomerProfile]:
        """All profiles, for maintenance jobs such as the KEK re-wrap (PD-14)."""
        with self._lock:
            if status is None:
                rows = self._conn.execute("SELECT * FROM profiles").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM profiles WHERE status = ?", (status.value,)
                ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def update_wrapped_dek(self, customer_id: str, wrapped_dek: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE profiles SET wrapped_dek = ?, updated_at = ? WHERE customer_id = ?",
                (wrapped_dek, _iso(utc_now()), customer_id),
            )
            self._conn.commit()

    # -- payments (PD-16) -----------------------------------------------------

    def record_payment(
        self,
        transaction_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        currency: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO payments (
                    transaction_id, customer_id, merchant_id,
                    amount_minor, currency, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    customer_id,
                    merchant_id,
                    amount_minor,
                    currency,
                    _iso(utc_now()),
                ),
            )
            self._conn.commit()

    def get_payment(self, transaction_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM payments WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_refund(self, transaction_id: str, amount_minor: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE payments SET refunded_minor = refunded_minor + ? "
                "WHERE transaction_id = ?",
                (amount_minor, transaction_id),
            )
            self._conn.commit()

    def mark_voided(self, transaction_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE payments SET voided = 1 WHERE transaction_id = ?",
                (transaction_id,),
            )
            self._conn.commit()

    # -- low-value exemption counters (PD-08) ---------------------------------

    def low_value_usage(self, customer_id: str) -> tuple[int, int]:
        """Return (cumulative minor units, transaction count) since last SCA."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cumulative_minor, transaction_count FROM low_value_usage "
                "WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["cumulative_minor"]), int(row["transaction_count"])

    def record_low_value_use(self, customer_id: str, amount_minor: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO low_value_usage (
                    customer_id, cumulative_minor, transaction_count, updated_at
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(customer_id) DO UPDATE SET
                    cumulative_minor = cumulative_minor + excluded.cumulative_minor,
                    transaction_count = transaction_count + 1,
                    updated_at = excluded.updated_at
                """,
                (customer_id, amount_minor, _iso(utc_now())),
            )
            self._conn.commit()

    def reset_low_value_use(self, customer_id: str) -> None:
        """Clear the allowance. Called after a strongly authenticated charge."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM low_value_usage WHERE customer_id = ?", (customer_id,)
            )
            self._conn.commit()

    # -- rate limiting (PD-07) ------------------------------------------------

    def hit_rate_limit(self, bucket: str, window_start: str, limit: int) -> bool:
        """Count one attempt. Returns True if the caller is now over ``limit``.

        Counts first and compares after, so a burst of concurrent requests
        cannot slip through by all reading the same pre-increment value.
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rate_limits (bucket, window_start, hits)
                VALUES (?, ?, 1)
                ON CONFLICT(bucket, window_start) DO UPDATE SET
                    hits = hits + 1
                """,
                (bucket, window_start),
            )
            row = self._conn.execute(
                "SELECT hits FROM rate_limits WHERE bucket = ? AND window_start = ?",
                (bucket, window_start),
            ).fetchone()
            self._conn.commit()
        return int(row["hits"]) > limit

    def purge_rate_limits(self, older_than: str) -> int:
        with self._lock:
            # CAST because window_start is TEXT: string comparison would order
            # "9" after "10", quietly retaining or deleting the wrong windows.
            cursor = self._conn.execute(
                "DELETE FROM rate_limits WHERE CAST(window_start AS INTEGER) < ?",
                (int(older_than),),
            )
            self._conn.commit()
            return cursor.rowcount

    # -- credentials ----------------------------------------------------------

    def create_credential(self, credential: CustomerCredential) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO credentials (
                    credential_id, customer_id, token_hash,
                    device_label, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    credential.credential_id,
                    credential.customer_id,
                    credential.token_hash,
                    credential.device_label,
                    _iso(credential.created_at),
                    _iso(credential.revoked_at) if credential.revoked_at else None,
                ),
            )
            self._conn.commit()

    def active_credentials(self, customer_id: str) -> list[CustomerCredential]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM credentials
                WHERE customer_id = ? AND revoked_at IS NULL
                ORDER BY created_at
                """,
                (customer_id,),
            ).fetchall()
        return [
            CustomerCredential(
                credential_id=row["credential_id"],
                customer_id=row["customer_id"],
                token_hash=row["token_hash"],
                device_label=row["device_label"],
                created_at=_parse_dt(row["created_at"]) or utc_now(),
                revoked_at=_parse_dt(row["revoked_at"]),
            )
            for row in rows
        ]

    def revoke_credentials(self, customer_id: str) -> int:
        """Revoke every credential for a customer.

        Called on erasure: the profile is gone, so any device still holding a
        credential for it must stop being able to authenticate.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE credentials SET revoked_at = ? "
                "WHERE customer_id = ? AND revoked_at IS NULL",
                (_iso(utc_now()), customer_id),
            )
            self._conn.commit()
            return cursor.rowcount

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
