"""Customer account management (PD-22, PD-28).

Everything a customer does to their own account after enrollment. Split from
``EnrollmentService`` because enrollment happens once and these happen for
years afterwards; they have different callers and different failure modes.

Consent withdrawal is treated as separate from erasure on purpose. They are
different rights: a customer may want to stop the biometric processing without
destroying the record. Withdrawal therefore *suspends* -- the palm stops being
usable immediately, but the data survives so the customer can change their mind
or export it. How long a suspended profile may be retained before erasure
becomes mandatory is a legal question (PD-01), not one to invent a number for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..crypto.envelope import CustomerCipher, DecryptionError
from ..crypto.kms import KeyDestroyedError, KeyManager, WrappedKey, dek_aad
from ..payments.gateway import CardDetails, CardToken, PaymentError, PaymentGateway
from ..store.models import ProfileStatus
from ..store.repository import Repository
from .enrollment import FIELD_PAYMENT_TOKEN, ConsentGrant, REQUIRED_PURPOSES


class AccountError(Exception):
    """Account operation refused. ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class AccountService:
    repository: Repository
    kms: KeyManager
    gateway: PaymentGateway

    # -- consent (PD-22) ------------------------------------------------------

    def withdraw_consent(self, customer_id: str) -> None:
        """Stop biometric processing without destroying the data."""
        profile = self.repository.get_profile(customer_id)
        if profile is None:
            raise AccountError("not_found", "unknown customer")
        if profile.status is ProfileStatus.SHREDDED:
            raise AccountError("already_erased", "this profile has been erased")

        self.repository.set_profile_status(customer_id, ProfileStatus.SUSPENDED)
        self.repository.withdraw_consent(customer_id)
        self.repository.append_audit(
            event_type="consent_withdrawn",
            outcome="success",
            customer_id=customer_id,
            detail={"profile_status": ProfileStatus.SUSPENDED.value, "data_retained": True},
        )

    def restore_consent(self, customer_id: str, consent: ConsentGrant) -> None:
        """Re-consent and reactivate a suspended profile.

        Requires a fresh, complete grant rather than reviving the old one --
        consent that was withdrawn is spent, and the customer must be shown the
        current policy version to give it again.
        """
        profile = self.repository.get_profile(customer_id)
        if profile is None:
            raise AccountError("not_found", "unknown customer")
        if profile.status is ProfileStatus.SHREDDED:
            raise AccountError(
                "already_erased",
                "this profile was erased; its key is gone and it cannot be restored",
            )
        if not consent.granted:
            raise AccountError("consent_required", "explicit consent was not granted")
        missing = [p for p in REQUIRED_PURPOSES if p not in consent.purposes]
        if missing:
            raise AccountError(
                "consent_incomplete",
                "consent does not cover all required purposes",
                {"missing_purposes": missing},
            )

        from uuid import uuid4

        from ..store.models import ConsentRecord

        self.repository.record_consent(
            ConsentRecord(
                consent_id=f"con_{uuid4().hex[:20]}",
                customer_id=customer_id,
                purposes=tuple(consent.purposes),
                policy_version=consent.policy_version,
                evidence_digest=consent.digest(),
            )
        )
        self.repository.set_profile_status(customer_id, ProfileStatus.ACTIVE)
        self.repository.append_audit(
            event_type="consent_restored",
            outcome="success",
            customer_id=customer_id,
            detail={"policy_version": consent.policy_version},
        )

    # -- card (PD-28) ---------------------------------------------------------

    def replace_card(self, customer_id: str, card: CardDetails) -> CardToken:
        """Swap the customer's one card without re-scanning their palm.

        A card expires or is reissued far more often than a palm changes, so
        tying the two together would mean a biometric re-enrollment every few
        years for a purely financial event.

        The palm template is untouched: only the payment-token field is
        resealed, under the same DEK.
        """
        profile = self.repository.get_profile(customer_id)
        if profile is None:
            raise AccountError("not_found", "unknown customer")
        if profile.status is ProfileStatus.SHREDDED:
            raise AccountError("already_erased", "this profile has been erased")
        if not profile.wrapped_dek:
            raise AccountError("profile_unavailable", "customer key material is unavailable")

        try:
            token = self.gateway.tokenize(card, customer_id)
        except PaymentError as exc:
            raise AccountError("card_rejected", str(exc)) from exc

        try:
            wrapped = WrappedKey.deserialize(profile.wrapped_dek)
            dek = self.kms.unwrap_dek(wrapped, dek_aad(customer_id))
        except (KeyDestroyedError, ValueError) as exc:
            raise AccountError("profile_unavailable", "customer key is unreadable") from exc

        cipher = CustomerCipher(customer_id=customer_id, dek=dek)
        try:
            sealed = cipher.seal(
                FIELD_PAYMENT_TOKEN,
                json.dumps(token.to_payload(), separators=(",", ":")).encode("utf-8"),
            )
        except DecryptionError as exc:  # pragma: no cover - sealing does not decrypt
            raise AccountError("profile_unavailable", str(exc)) from exc
        finally:
            cipher.close()

        self.repository.update_payment_token(customer_id, sealed)
        self.repository.append_audit(
            event_type="card_replaced",
            outcome="success",
            customer_id=customer_id,
            detail={"card": token.display(), "scheme": token.scheme.value},
        )
        return token
