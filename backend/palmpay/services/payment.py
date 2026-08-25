"""Identification and charging: the walk-up-and-pay path.

This is the resolution of section 6.2 of the design document, the encrypted
1:N matching tension. The chosen answer is 1:small-N:

* The identifier hint maps to a shard. Only profiles in that shard are
  candidates, so the number of comparisons is bounded by shard occupancy
  rather than by total enrollment.
* Each candidate DEK is unwrapped individually and used to decrypt only that
  candidate's template. Per-customer encryption is therefore fully preserved:
  there is no shared template-index key and no plaintext template store.
* Plaintext DEKs and templates live only inside this call and are zeroized on
  the way out.

The cost is honest and bounded: identification does O(shard size) unwrap and
decrypt operations. That is precisely why ``max_candidates`` exists, and why a
shard that grows past it is treated as a configuration failure rather than
quietly truncated.

The accuracy argument is the stronger one. False-match probability accumulates
with every comparison, so a true 1:N search over a million enrolled palms is
far more dangerous for payments than the same matcher run against a few dozen
candidates. Narrowing first is what makes the false-accept budget survivable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np

from ..palmprint.liveness import LivenessConfig, LivenessReport, assess_liveness
from ..palmprint.registry import BiometricEngine
from ..palmprint.types import Template
from ..config import Settings
from ..crypto.envelope import CustomerCipher, DecryptionError
from ..crypto.kms import KeyDestroyedError, KeyManager, WrappedKey, dek_aad
from ..payments.gateway import (
    AuthorizationRequest,
    AuthorizationResult,
    CardToken,
    CardScheme,
    PaymentError,
    PaymentGateway,
)
from ..payments.sca import (
    HintType,
    LowValueTracker,
    SCAAssessment,
    assess,
    hint_factor,
    palm_factor,
)
from ..store.models import CustomerProfile, shard_key
from ..store.repository import Repository
from .enrollment import FIELD_PAYMENT_TOKEN, FIELD_TEMPLATE
import json


class PaymentDeclined(Exception):
    """The transaction was refused. ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class IdentificationResult:
    profile: CustomerProfile
    distance: float
    runner_up_distance: float | None
    candidates_considered: int

    @property
    def margin(self) -> float:
        if self.runner_up_distance is None:
            return float("inf")
        return self.runner_up_distance - self.distance


@dataclass
class PaymentOutcome:
    customer_id: str
    authorization: AuthorizationResult
    sca: SCAAssessment
    distance: float
    margin: float
    candidates_considered: int
    card_display: str


@dataclass
class PaymentService:
    repository: Repository
    kms: KeyManager
    engine: BiometricEngine
    gateway: PaymentGateway
    settings: Settings
    liveness_config: LivenessConfig = field(default_factory=LivenessConfig)
    low_value_tracker: LowValueTracker = field(default_factory=LowValueTracker)

    # -- capture --------------------------------------------------------------

    def _probe_template(self, frame: np.ndarray) -> tuple[Template, LivenessReport | None]:
        liveness: LivenessReport | None = None
        if self.settings.require_liveness:
            liveness = assess_liveness(frame, self.liveness_config)
            if not liveness.passed:
                raise PaymentDeclined(
                    "liveness_failed",
                    "the presented palm did not pass the liveness check",
                    {"liveness": liveness.as_dict()},
                )

        roi = self.engine.region_extractor.locate(frame)
        if roi is None:
            raise PaymentDeclined("palm_not_found", "no palm could be located in the frame")
        if self.settings.require_quality and not roi.quality.ok:
            raise PaymentDeclined(
                "poor_quality",
                "the capture was not good enough to identify a palm",
                {"quality": roi.quality.as_dict()},
            )

        return self.engine.feature_extractor.extract(roi), liveness

    # -- identification -------------------------------------------------------

    def _open_profile(self, profile: CustomerProfile) -> CustomerCipher | None:
        """Unwrap one candidate DEK. ``None`` means the customer was shredded."""
        if not profile.wrapped_dek:
            return None
        try:
            wrapped = WrappedKey.deserialize(profile.wrapped_dek)
            dek = self.kms.unwrap_dek(wrapped, dek_aad(profile.customer_id))
        except (KeyDestroyedError, ValueError):
            return None
        return CustomerCipher(customer_id=profile.customer_id, dek=dek)

    def identify(self, probe: Template, hint: str) -> IdentificationResult:
        shard = shard_key(self.settings.resolve_pepper(), hint)
        shard_size = self.repository.count_in_shard(shard)
        if shard_size > self.settings.max_candidates:
            # The hint has stopped narrowing. Matching the first N would hide a
            # real accuracy regression behind a plausible-looking success.
            raise PaymentDeclined(
                "shard_overflow",
                "too many enrolled palms share this identifier",
                {"shard_size": shard_size, "limit": self.settings.max_candidates},
            )

        candidates = self.repository.find_candidates(shard, self.settings.max_candidates)
        if not candidates:
            raise PaymentDeclined("no_match", "no enrolled palm matches this identifier")

        scored: list[tuple[float, CustomerProfile]] = []
        for profile in candidates:
            if profile.engine_id != self.engine.engine_id:
                # Enrolled on a different engine build; comparing would be
                # meaningless, so this candidate is skipped.
                #
                # This is a stopgap. The customer silently stops being able to
                # pay and nothing says why -- the skip is not counted in the
                # audit record. A real engine upgrade needs a migration and a
                # re-enrollment prompt. See BACKLOG PD-13.
                continue
            cipher = self._open_profile(profile)
            if cipher is None:
                continue
            try:
                stored = Template.deserialize(cipher.open(FIELD_TEMPLATE, profile.enc_template))
            except (DecryptionError, ValueError):
                continue
            finally:
                cipher.close()

            scored.append((self.engine.matcher.distance(probe, stored), profile))

        if not scored:
            raise PaymentDeclined(
                "no_match",
                "no usable enrolled palm matches this identifier",
                {"candidates_considered": len(candidates)},
            )

        scored.sort(key=lambda item: item[0])
        best_distance, best_profile = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else None

        if best_distance > self.engine.matcher.threshold:
            raise PaymentDeclined(
                "no_match",
                "the presented palm does not match any enrolled palm",
                {
                    "distance": round(best_distance, 5),
                    "threshold": self.engine.matcher.threshold,
                    "candidates_considered": len(scored),
                },
            )

        result = IdentificationResult(
            profile=best_profile,
            distance=best_distance,
            runner_up_distance=runner_up,
            candidates_considered=len(scored),
        )

        # Two enrolled palms both look like this one. Charging either would be
        # a coin flip with someone's money, so refuse and fall back to another
        # payment method.
        if result.margin < self.settings.match_margin:
            raise PaymentDeclined(
                "ambiguous_match",
                "more than one enrolled palm matches too closely",
                {
                    "distance": round(best_distance, 5),
                    "runner_up": round(runner_up, 5) if runner_up is not None else None,
                    "required_margin": self.settings.match_margin,
                },
            )

        return result

    # -- payment --------------------------------------------------------------

    def load_card_token(self, profile: CustomerProfile) -> CardToken:
        cipher = self._open_profile(profile)
        if cipher is None:
            raise PaymentDeclined("profile_unavailable", "customer key material is unavailable")
        try:
            payload = json.loads(cipher.open(FIELD_PAYMENT_TOKEN, profile.enc_payment_token))
        except (DecryptionError, ValueError) as exc:
            raise PaymentDeclined("profile_unavailable", "stored payment token is unreadable") from exc
        finally:
            cipher.close()

        return CardToken(
            token=payload["token"],
            scheme=CardScheme(payload["scheme"]),
            last4=payload["last4"],
            exp_month=int(payload["exp_month"]),
            exp_year=int(payload["exp_year"]),
            scheme_reference=payload.get("scheme_reference", ""),
        )

    def pay(
        self,
        *,
        frame: np.ndarray,
        hint: str,
        amount_minor: int,
        currency: str,
        merchant_id: str,
        idempotency_key: str | None = None,
        description: str = "",
    ) -> PaymentOutcome:
        try:
            probe, liveness = self._probe_template(frame)
            identification = self.identify(probe, hint)
        except PaymentDeclined as exc:
            self.repository.append_audit(
                event_type="payment",
                outcome="declined",
                merchant_id=merchant_id,
                detail={"code": exc.code, **exc.detail, "amount_minor": amount_minor},
            )
            raise

        profile = identification.profile

        # Build the SCA case. The palm is inherence; the hint contributes a
        # knowledge factor only if it is a customer secret (see payments/sca.py).
        factors = [
            palm_factor(
                distance=identification.distance,
                threshold=self.engine.matcher.threshold,
                liveness_passed=liveness.passed if liveness else True,
            )
        ]
        knowledge = hint_factor(HintType(profile.hint_type))
        if knowledge is not None:
            factors.append(knowledge)

        assessment = assess(
            factors,
            customer_id=profile.customer_id,
            amount_minor=amount_minor,
            tracker=self.low_value_tracker,
        )

        token = self.load_card_token(profile)
        request = AuthorizationRequest(
            amount_minor=amount_minor,
            currency=currency,
            merchant_id=merchant_id,
            token=token,
            customer_id=profile.customer_id,
            sca=assessment,
            idempotency_key=idempotency_key or f"idem_{uuid.uuid4().hex}",
            description=description,
        )

        try:
            authorization = self.gateway.authorize(request)
        except PaymentError as exc:
            self.repository.append_audit(
                event_type="payment",
                outcome="error",
                customer_id=profile.customer_id,
                merchant_id=merchant_id,
                detail={"error": str(exc), "amount_minor": amount_minor},
            )
            raise PaymentDeclined("gateway_error", str(exc)) from exc

        # Keep the PSD2 low-value counters honest: a strongly authenticated
        # transaction resets the allowance, an exempt one consumes it.
        if authorization.approved:
            if assessment.strongly_authenticated:
                self.low_value_tracker.reset(profile.customer_id)
            elif assessment.exemption.value == "low_value":
                self.low_value_tracker.record_exempt(profile.customer_id, amount_minor)

        self.repository.append_audit(
            event_type="payment",
            outcome=authorization.status.value,
            customer_id=profile.customer_id,
            merchant_id=merchant_id,
            detail={
                "amount_minor": amount_minor,
                "currency": currency,
                "transaction_id": authorization.transaction_id,
                "decline_reason": (
                    authorization.decline_reason.value if authorization.decline_reason else None
                ),
                "distance": round(identification.distance, 5),
                "margin": (
                    round(identification.margin, 5)
                    if identification.runner_up_distance is not None
                    else None
                ),
                "candidates_considered": identification.candidates_considered,
                "sca": assessment.as_dict(),
            },
        )

        return PaymentOutcome(
            customer_id=profile.customer_id,
            authorization=authorization,
            sca=assessment,
            distance=identification.distance,
            margin=identification.margin,
            candidates_considered=identification.candidates_considered,
            card_display=token.display(),
        )
