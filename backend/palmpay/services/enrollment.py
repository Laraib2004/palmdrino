"""Enrollment: the one-time step that makes palm payment possible afterwards.

Enrollment is where the design document's "signup-free payment" is paid for.
Everything expensive happens once, here: consent, capture quality, template
creation, card tokenisation, key generation.

Order of operations matters and is deliberate:

1. Consent is checked *first*. Processing biometric data without a valid
   Art. 9 basis is unlawful, so no frame is analysed before consent exists.
2. Liveness and quality gate every frame. A weak enrollment template poisons
   every future match, and unlike a bad password it cannot be reset -- the
   customer would have to re-enroll.
3. Multiple samples are required and cross-checked, so a single unlucky ROI
   cannot become someone's permanent identity.
4. Only then is a DEK generated and data sealed.

The raw frames exist as function arguments and are never written anywhere.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

import numpy as np

from ..palmprint.liveness import LivenessConfig, assess_liveness
from ..palmprint.registry import BiometricEngine
from ..palmprint.types import QualityReport, Template
from ..config import Settings
from ..crypto.envelope import CustomerCipher
from ..crypto.kms import KeyManager, dek_aad, zeroize
from ..payments.gateway import CardDetails, CardToken, PaymentError, PaymentGateway
from ..payments.sca import HintType
from ..store.models import ConsentRecord, CustomerProfile, shard_key
from ..store.repository import Repository

FIELD_TEMPLATE = "biometric_template"
FIELD_PAYMENT_TOKEN = "payment_token"
FIELD_PII = "pii"

REQUIRED_PURPOSES = ("biometric_processing", "payment_execution")


class EnrollmentError(Exception):
    """Enrollment refused. ``code`` is stable and safe to show a client."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class ConsentGrant:
    """Explicit consent as captured by the enrollment UI."""

    granted: bool
    purposes: tuple[str, ...]
    policy_version: str
    evidence_text: str = ""

    def digest(self) -> str:
        """Hash of the exact wording the customer agreed to.

        Storing the digest rather than the text keeps the consent record small
        while still letting you prove, later, precisely which version of the
        notice was shown.
        """
        payload = json.dumps(
            {
                "purposes": list(self.purposes),
                "policy_version": self.policy_version,
                "evidence": self.evidence_text,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class EnrollmentResult:
    customer_id: str
    engine_id: str
    card_display: str
    card_scheme: str
    hint_type: HintType
    sample_count: int
    max_pairwise_distance: float
    quality: list[dict] = field(default_factory=list)


@dataclass
class EnrollmentService:
    repository: Repository
    kms: KeyManager
    engine: BiometricEngine
    gateway: PaymentGateway
    settings: Settings
    liveness_config: LivenessConfig = field(default_factory=LivenessConfig)

    # -- consent --------------------------------------------------------------

    def _check_consent(self, consent: ConsentGrant) -> None:
        if not consent.granted:
            raise EnrollmentError("consent_required", "explicit consent was not granted")
        missing = [p for p in REQUIRED_PURPOSES if p not in consent.purposes]
        if missing:
            raise EnrollmentError(
                "consent_incomplete",
                "consent does not cover all required purposes",
                {"missing_purposes": missing},
            )
        if not consent.policy_version:
            raise EnrollmentError("consent_incomplete", "policy version is required")

    # -- biometrics -----------------------------------------------------------

    def _template_from_frame(self, frame: np.ndarray, index: int) -> tuple[Template, QualityReport]:
        if self.settings.require_liveness:
            liveness = assess_liveness(frame, self.liveness_config)
            if not liveness.passed:
                raise EnrollmentError(
                    "liveness_failed",
                    f"sample {index} did not pass the liveness check",
                    {"sample": index, "liveness": liveness.as_dict()},
                )

        roi = self.engine.region_extractor.locate(frame)
        if roi is None:
            raise EnrollmentError(
                "palm_not_found", f"no palm could be located in sample {index}", {"sample": index}
            )
        if self.settings.require_quality and not roi.quality.ok:
            raise EnrollmentError(
                "poor_quality",
                f"sample {index} failed the quality gate",
                {"sample": index, "quality": roi.quality.as_dict()},
            )

        return self.engine.feature_extractor.extract(roi), roi.quality

    def _select_reference(self, templates: list[Template]) -> tuple[Template, float]:
        """Pick the medoid sample and report the worst pairwise distance.

        The medoid -- the sample closest on average to all the others -- is the
        most representative capture of the set. Taking the first sample instead
        would let one bad-but-passing frame become the stored identity.
        """
        count = len(templates)
        if count == 1:
            return templates[0], 0.0

        distances = np.zeros((count, count), dtype=np.float64)
        for i in range(count):
            for j in range(i + 1, count):
                d = self.engine.matcher.distance(templates[i], templates[j])
                distances[i, j] = distances[j, i] = d

        worst = float(distances.max())
        medoid = int(np.argmin(distances.sum(axis=1)))
        return templates[medoid], worst

    # -- public API -----------------------------------------------------------

    def enroll(
        self,
        *,
        frames: list[np.ndarray],
        hint: str,
        hint_type: HintType,
        card: CardDetails,
        pii: dict,
        consent: ConsentGrant,
    ) -> EnrollmentResult:
        self._check_consent(consent)

        expected = self.settings.enrollment_samples
        if len(frames) < expected:
            raise EnrollmentError(
                "insufficient_samples",
                f"{expected} palm samples are required, got {len(frames)}",
                {"required": expected, "received": len(frames)},
            )

        templates: list[Template] = []
        qualities: list[dict] = []
        for index, frame in enumerate(frames, start=1):
            template, quality = self._template_from_frame(frame, index)
            templates.append(template)
            qualities.append(quality.as_dict())

        reference, worst_distance = self._select_reference(templates)
        if worst_distance > self.settings.enrollment_consistency_max:
            raise EnrollmentError(
                "inconsistent_samples",
                "the palm samples do not agree with each other",
                {
                    "max_pairwise_distance": round(worst_distance, 5),
                    "limit": self.settings.enrollment_consistency_max,
                },
            )

        customer_id = f"cus_{uuid.uuid4().hex[:20]}"

        # Tokenise before generating keys: if the card is rejected there is no
        # half-built profile or orphaned key material to clean up.
        try:
            token = self.gateway.tokenize(card, customer_id)
        except PaymentError as exc:
            raise EnrollmentError("card_rejected", str(exc)) from exc

        shard = shard_key(self.settings.resolve_pepper(), hint)
        profile = self._seal_profile(
            customer_id=customer_id,
            shard=shard,
            hint_type=hint_type,
            template=reference,
            token=token,
            pii=pii,
        )

        self.repository.create_profile(profile)
        self.repository.record_consent(
            ConsentRecord(
                consent_id=f"con_{uuid.uuid4().hex[:20]}",
                customer_id=customer_id,
                purposes=tuple(consent.purposes),
                policy_version=consent.policy_version,
                evidence_digest=consent.digest(),
            )
        )
        self.repository.append_audit(
            event_type="enrollment",
            outcome="success",
            customer_id=customer_id,
            detail={
                "engine_id": self.engine.engine_id,
                "samples": len(frames),
                "max_pairwise_distance": round(worst_distance, 5),
                "card": token.display(),
                "hint_type": hint_type.value,
                "shard_size_after": self.repository.count_in_shard(shard),
            },
        )

        return EnrollmentResult(
            customer_id=customer_id,
            engine_id=self.engine.engine_id,
            card_display=token.display(),
            card_scheme=token.scheme.value,
            hint_type=hint_type,
            sample_count=len(frames),
            max_pairwise_distance=round(worst_distance, 5),
            quality=qualities,
        )

    def _seal_profile(
        self,
        *,
        customer_id: str,
        shard: str,
        hint_type: HintType,
        template: Template,
        token: CardToken,
        pii: dict,
    ) -> CustomerProfile:
        """Generate the customer DEK and seal everything under it."""
        dek = self.kms.generate_dek()
        try:
            wrapped = self.kms.wrap_dek(bytes(dek), dek_aad(customer_id))
            cipher = CustomerCipher(customer_id=customer_id, dek=bytearray(dek))
            try:
                enc_template = cipher.seal(FIELD_TEMPLATE, template.serialize())
                enc_token = cipher.seal(
                    FIELD_PAYMENT_TOKEN,
                    json.dumps(
                        {
                            "token": token.token,
                            "scheme": token.scheme.value,
                            "last4": token.last4,
                            "exp_month": token.exp_month,
                            "exp_year": token.exp_year,
                            "scheme_reference": token.scheme_reference,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
                enc_pii = cipher.seal(
                    FIELD_PII, json.dumps(pii, separators=(",", ":")).encode("utf-8")
                )
            finally:
                cipher.close()
        finally:
            zeroize(dek)

        return CustomerProfile(
            customer_id=customer_id,
            shard=shard,
            engine_id=self.engine.engine_id,
            wrapped_dek=wrapped.serialize(),
            enc_template=enc_template,
            enc_payment_token=enc_token,
            enc_pii=enc_pii,
            hint_type=hint_type.value,
        )

    def delete_customer(self, customer_id: str) -> bool:
        """Erase a customer by crypto-shred.

        Destroying the wrapped DEK is the erasure. Consent proof and
        pseudonymised audit entries survive on purpose: they contain no
        biometric data and no PII, and they are the evidence that the
        processing which did happen was lawful.
        """
        shredded = self.repository.crypto_shred(customer_id)
        if shredded:
            self.repository.withdraw_consent(customer_id)
            self.repository.append_audit(
                event_type="erasure",
                outcome="success",
                customer_id=customer_id,
                detail={"method": "crypto_shred", "dek_destroyed": True},
            )
        return shredded
