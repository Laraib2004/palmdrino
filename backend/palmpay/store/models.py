"""Persistence records.

Note what is *not* here: no raw palm image, no PAN, no CVV, and no plaintext
identifier hint. Raw frames are held in memory for the duration of a request
and discarded; card data never reaches this system because the gateway
tokenises it; and the hint is stored only as a keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProfileStatus(str, Enum):
    ACTIVE = "active"
    # PD-22: consent withdrawn, biometric processing stopped, data still
    # present. Distinct from SHREDDED because withdrawing consent and erasing
    # data are different rights and a customer may want the first without the
    # second. How long a suspended profile may be retained before erasure is a
    # question for PD-01, not one to guess at here.
    SUSPENDED = "suspended"
    SHREDDED = "shredded"


def shard_key(pepper: bytes, hint: str) -> str:
    """Derive the search shard for an identifier hint.

    The hint (phone last-4, a short member number) is what turns 1:N into
    1:small-N. Storing it in the clear would hand an attacker with database
    access a directory of who is enrolled, so only this keyed hash is kept.

    Honest limitation: a 4-digit hint has ~10^4 possible values. The pepper
    stops an attacker who has only the database from enumerating it, but not
    one who also has the pepper. That is why the pepper must live in a
    different trust domain than the database, and why the hint is a search
    narrowing device only -- it is never treated as an authentication factor.
    The palm is the authentication factor.
    """
    normalised = "".join(ch for ch in hint if ch.isalnum()).lower()
    if not normalised:
        raise ValueError("identifier hint is empty after normalisation")
    return hmac.new(pepper, normalised.encode("utf-8"), hashlib.sha256).hexdigest()


def credential_hash(pepper: bytes, secret: str) -> str:
    """Hash a device credential for storage.

    A plain keyed hash rather than a password KDF, deliberately: the secret is
    32 random bytes issued by the server, not something a human chose. There is
    no dictionary to attack and nothing for bcrypt-style stretching to buy. What
    matters is that the database never holds the usable value.

    Domain-separated from ``shard_key`` so the same pepper cannot produce a
    collision between a hint hash and a credential hash.
    """
    return hmac.new(
        pepper, f"credential:v1:{secret}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass
class CustomerCredential:
    """Proves a caller is the customer they claim to be.

    Issued once at enrollment and bound to the device that enrolled. Stored as
    a hash only -- a database leak yields nothing that can be replayed.

    Modelled as its own record rather than a column on the profile so that a
    second device, or rotation after a suspected compromise, is an insert and a
    revoke rather than a schema change.
    """

    credential_id: str
    customer_id: str
    token_hash: str
    device_label: str = ""
    created_at: datetime = field(default_factory=utc_now)
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


@dataclass
class PalmTemplate:
    """One enrolled hand (PD-21).

    A customer may enrol more than one palm, so that a bandaged or injured hand
    does not leave them unable to pay. Templates live in their own table rather
    than as a column on the profile because there is genuinely more than one.

    ``engine_id`` is authoritative here rather than on the profile: an engine
    upgrade can be rolled out hand by hand, and only templates from the current
    engine are comparable.
    """

    template_id: str
    customer_id: str
    engine_id: str
    enc_template: bytes
    label: str = "primary"
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class CustomerProfile:
    """An enrolled customer.

    Every ``enc_*`` field is sealed under this customer's DEK. Deleting
    ``wrapped_dek`` makes all of them permanently unreadable -- that single
    delete is the crypto-shred.
    """

    customer_id: str
    shard: str
    engine_id: str
    wrapped_dek: bytes
    enc_payment_token: bytes
    enc_pii: bytes
    # Whether the identifier hint is a user-chosen secret or a public
    # identifier. Stored in the clear because it is a policy flag, not a
    # secret, and the SCA assessment must not be able to mistake one for the
    # other -- see payments/sca.py.
    hint_type: str = "public"
    status: ProfileStatus = ProfileStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def is_active(self) -> bool:
        return self.status is ProfileStatus.ACTIVE


@dataclass
class ConsentRecord:
    """Proof that explicit GDPR Art. 9 consent was given.

    Deliberately survives erasure. Deleting the proof of consent along with the
    data would destroy the legal basis for having processed it, so this record
    contains no biometric data and no PII beyond the pseudonymous customer id
    -- only what is needed to demonstrate that consent existed, for which
    purposes, and under which policy version.
    """

    consent_id: str
    customer_id: str
    purposes: tuple[str, ...]
    policy_version: str
    granted_at: datetime = field(default_factory=utc_now)
    withdrawn_at: datetime | None = None
    evidence_digest: str = ""

    @property
    def is_active(self) -> bool:
        return self.withdrawn_at is None


@dataclass
class AuditEvent:
    """Pseudonymised operational log entry.

    References a customer id, never a template, never an image, never a PAN.
    """

    event_id: str
    event_type: str
    customer_id: str | None
    merchant_id: str | None
    outcome: str
    detail: dict
    created_at: datetime = field(default_factory=utc_now)
