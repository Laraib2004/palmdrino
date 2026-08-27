"""HTTP API consumed by the Android enrollment and terminal apps.

Images arrive as multipart uploads, are decoded in memory, used, and dropped.
No endpoint writes a frame to disk and none returns a template.

Run it::

    py -3.13 -m uvicorn palmpay.api.main:app --reload --host 0.0.0.0 --port 8000

Interactive docs at ``/docs``.

TRANSPORT SECURITY
------------------
Every request here carries either biometric data or a payment instruction.
This app must sit behind TLS -- and for the terminal endpoints, mutual TLS or
device attestation, since a terminal that can call ``/v1/pay`` can charge
customers. The API-key check below is a minimum, not a substitute for that.
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..palmprint.liveness import assess_liveness
from ..crypto.envelope import DecryptionError
from ..payments.gateway import CardDetails, PaymentError
from ..payments.sca import HintType
import hashlib
from ..services.container import ServiceContainer
from ..services.account import AccountError
from ..services.credentials import AuthenticationError
from ..services.enrollment import ConsentGrant, EnrollmentError
from ..services.ratelimit import RateLimited
from ..services.payment import PaymentDeclined
from ..store.models import ProfileStatus
from . import schemas

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 8

_container: ServiceContainer | None = None


def container() -> ServiceContainer:
    if _container is None:  # pragma: no cover - guarded by lifespan
        raise RuntimeError("service container is not initialised")
    return _container


def set_container(services: ServiceContainer | None) -> None:
    """Inject a pre-built container.

    Lets tests point the app at a temporary data directory and a gateway they
    can inspect. When one is injected the app neither rebuilds nor closes it --
    ownership stays with whoever supplied it.
    """
    global _container
    _container = services


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _container
    injected = _container is not None
    if not injected:
        _container = ServiceContainer.build()
    try:
        yield
    finally:
        if not injected:
            _container.close()
            _container = None


app = FastAPI(
    title="Palmdrino Palm Payment API",
    version="0.1.0",
    description=(
        "Prototype palm-biometric payment service. Enrollment links a palm to a "
        "tokenised Visa/Mastercard; payment identifies the palm within an "
        "identifier-narrowed shard and charges the card on file."
    ),
    lifespan=lifespan,
)


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Terminal grant: authorises taking payments.

    Open by default so the prototype runs with no setup. Set ``PALMPAY_API_KEY``
    and it is enforced -- which any deployment reachable by anything other than
    localhost must do.

    This grant deliberately cannot reach customer account endpoints. It is held
    by merchant terminals, of which there are few and which are trusted; a
    customer's account is not something a terminal has any business reading.
    """
    expected = os.environ.get("PALMPAY_API_KEY")
    if not expected:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "invalid API key"},
        )


async def require_admin_key(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    """Admin grant: authorises reading the audit log.

    Separate from the terminal key because under a customer-facing app the
    terminal key is comparatively widely held, and the audit log is a record of
    who paid what, where. Set ``PALMPAY_ADMIN_KEY`` to enforce.
    """
    expected = os.environ.get("PALMPAY_ADMIN_KEY")
    if not expected:
        return
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "invalid admin key"},
        )


async def require_customer(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Customer grant: proves the caller is a specific enrolled customer.

    Returns the authenticated customer id. Unlike the two key checks above this
    is never optional -- there is no deployment in which reading or erasing a
    customer account should be open.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "unauthenticated",
                "message": "a customer credential is required",
            },
        )
    try:
        return container().credentials.verify(authorization[7:].strip())
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "message": "invalid credential"},
        ) from exc


def authorize_self(caller: str, customer_id: str) -> None:
    """Confirm an authenticated customer is acting on their own account.

    Answers 403 for any mismatch rather than 404, so the response cannot be used
    to probe which customer ids exist.
    """
    if caller != customer_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "this credential does not grant access to that customer",
            },
        )


def limit_identity(value: str) -> str:
    """Hash a value before it becomes a rate-limit bucket key.

    The pay code is one of the two SCA factors. Bucketing on it in the clear
    would turn the rate-limit table into a directory of live pay codes, so only
    a digest is ever stored.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def enforce_limit(scope: str, identity: str, merchant_id: str | None = None) -> None:
    """Apply a rate limit, answering 429 with Retry-After when exceeded."""
    services = container()
    try:
        services.rate_limiter.check(scope, identity)
    except RateLimited as exc:
        services.repository.append_audit(
            event_type="rate_limit",
            outcome="blocked",
            merchant_id=merchant_id,
            detail={"scope": exc.scope, "retry_after": exc.retry_after},
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": str(exc),
                "detail": {"retry_after": exc.retry_after},
            },
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


def decode_image(payload: bytes, label: str) -> np.ndarray:
    if not payload:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_image", "message": f"{label} is empty"},
        )
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "image_too_large", "message": f"{label} exceeds {MAX_IMAGE_BYTES} bytes"},
        )
    frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "undecodable_image", "message": f"{label} could not be decoded"},
        )
    return frame


GUIDANCE = {
    "too_blurry": "Hold your hand still",
    "too_dark": "Move somewhere brighter",
    "too_bright": "Move out of direct light",
    "low_contrast": "Move your hand slightly away from the camera",
    "palm_not_filling_frame": "Bring your palm closer and spread your fingers",
    "texture_too_flat": "Show your actual hand, not a photo",
    "specular_glare": "Reduce glare on your palm",
    "screen_moire": "Show your actual hand, not a screen",
    "chroma_too_uniform": "Show your actual hand, not a printed image",
}


@app.get("/v1/health", response_model=schemas.HealthResponse)
async def health() -> schemas.HealthResponse:
    services = container()
    settings = services.settings
    return schemas.HealthResponse(
        status="ok",
        engine_id=services.engine.engine_id,
        modality=settings.modality.value,
        enrolled_profiles=services.repository.count_profiles(ProfileStatus.ACTIVE),
        match_threshold=services.engine.matcher.threshold,
        liveness_required=settings.require_liveness,
        enrollment_samples=settings.enrollment_samples,
        gateway=services.gateway.name,
    )


@app.post(
    "/v1/capture/check",
    response_model=schemas.CaptureCheckResponse,
)
async def capture_check(
    image: Annotated[UploadFile, File(description="A single palm frame")],
) -> schemas.CaptureCheckResponse:
    """Score a frame without enrolling or charging anything.

    Open for the same reason as ``/v1/enroll``: the customer app calls this
    while the user frames their hand during self-enrollment, before any
    credential exists. It creates no template and writes nothing, but it does
    run segmentation and an FFT per call, which makes it a cheap
    CPU-exhaustion vector until PD-07 lands.
    """
    enforce_limit("capture_check", "global")
    services = container()
    frame = decode_image(await image.read(), "image")

    liveness = assess_liveness(frame)
    roi = services.engine.region_extractor.locate(frame)

    reasons: list[str] = list(liveness.reasons)
    quality = None
    if roi is not None:
        quality = schemas.QualityModel(**roi.quality.as_dict())
        reasons.extend(roi.quality.reasons)

    guidance = [GUIDANCE[r] for r in dict.fromkeys(reasons) if r in GUIDANCE]
    if roi is None:
        guidance.insert(0, "Show your open palm to the camera")

    return schemas.CaptureCheckResponse(
        usable=bool(roi is not None and roi.quality.ok and liveness.passed),
        palm_found=roi is not None,
        quality=quality,
        liveness=schemas.LivenessModel(**liveness.as_dict()),
        guidance=guidance,
    )


@app.post(
    "/v1/enroll",
    response_model=schemas.EnrollmentResponse,
)
async def enroll(
    frames: Annotated[list[UploadFile], File(description="Palm frames of the same hand")],
    hint: Annotated[str, Form(description="Identifier hint used to narrow matching")],
    hint_type: Annotated[str, Form(description="'secret' or 'public'")] = "secret",
    card_number: Annotated[str, Form()] = "",
    card_exp_month: Annotated[int, Form()] = 0,
    card_exp_year: Annotated[int, Form()] = 0,
    card_cvv: Annotated[str, Form()] = "",
    card_holder: Annotated[str, Form()] = "",
    pii: Annotated[str, Form(description="JSON object of minimal personal data")] = "{}",
    consent_granted: Annotated[bool, Form()] = False,
    consent_purposes: Annotated[str, Form(description="comma-separated")] = "",
    consent_policy_version: Annotated[str, Form()] = "",
    consent_evidence: Annotated[str, Form()] = "",
    device_label: Annotated[str, Form()] = "",
) -> schemas.EnrollmentResponse:
    """One-time enrollment: link a palm to a tokenised card.

    Deliberately unauthenticated: a customer enrolling themselves has no
    credential yet, and this is the call that mints one. That makes it the most
    exposed endpoint in the service and the first that needs rate limiting
    (PD-07).

    The card fields are accepted here only because this is a prototype. In
    production the PAN must be collected by a gateway-hosted field so it never
    reaches this service, which is what keeps PCI DSS scope small.
    """
    enforce_limit("enroll", limit_identity(hint))
    services = container()

    if len(frames) > MAX_FRAMES:
        raise HTTPException(
            status_code=400,
            detail={"code": "too_many_frames", "message": f"at most {MAX_FRAMES} frames"},
        )

    images = [decode_image(await item.read(), f"frame {index}") for index, item in enumerate(frames, 1)]

    try:
        parsed_pii = json.loads(pii) if pii else {}
        if not isinstance(parsed_pii, dict):
            raise ValueError("pii must be a JSON object")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_pii", "message": str(exc)}
        ) from exc

    try:
        card = CardDetails(
            pan=card_number,
            exp_month=card_exp_month,
            exp_year=card_exp_year,
            cvv=card_cvv,
            holder_name=card_holder,
        )
    except PaymentError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_card", "message": str(exc)}
        ) from exc

    try:
        parsed_hint_type = HintType(hint_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_hint_type", "message": "hint_type must be 'secret' or 'public'"},
        ) from exc

    consent = ConsentGrant(
        granted=consent_granted,
        purposes=tuple(p.strip() for p in consent_purposes.split(",") if p.strip()),
        policy_version=consent_policy_version,
        evidence_text=consent_evidence,
    )

    try:
        result = services.enrollment.enroll(
            frames=images,
            hint=hint,
            hint_type=parsed_hint_type,
            card=card,
            pii=parsed_pii,
            consent=consent,
            device_label=device_label,
        )
    except EnrollmentError as exc:
        status = 403 if exc.code.startswith("consent") else 422
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.EnrollmentResponse(
        customer_id=result.customer_id,
        credential=result.credential,
        engine_id=result.engine_id,
        card_display=result.card_display,
        card_scheme=result.card_scheme,
        hint_type=result.hint_type.value,
        sample_count=result.sample_count,
        max_pairwise_distance=result.max_pairwise_distance,
        quality=[schemas.QualityModel(**q) for q in result.quality],
    )


@app.post(
    "/v1/pay",
    response_model=schemas.PaymentResponse,
    dependencies=[Depends(require_api_key)],
)
async def pay(
    image: Annotated[UploadFile, File(description="A single palm frame")],
    hint: Annotated[str, Form()],
    amount_minor: Annotated[int, Form(description="Amount in minor units, e.g. cents")],
    merchant_id: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "EUR",
    idempotency_key: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> schemas.PaymentResponse:
    """Identify the presented palm and charge the linked card."""
    # Per pay code, so an attacker cannot grind one customer; and per terminal,
    # so a compromised terminal cannot grind everyone.
    enforce_limit("pay_by_hint", limit_identity(hint), merchant_id)
    enforce_limit("pay_by_terminal", limit_identity(merchant_id), merchant_id)
    services = container()
    frame = decode_image(await image.read(), "image")

    try:
        outcome = services.payment.pay(
            frame=frame,
            hint=hint,
            amount_minor=amount_minor,
            currency=currency,
            merchant_id=merchant_id,
            idempotency_key=idempotency_key or None,
            description=description,
        )
    except PaymentDeclined as exc:
        raise HTTPException(
            status_code=402,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc
    except PaymentError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "gateway_error", "message": str(exc)}
        ) from exc

    authorization = outcome.authorization
    return schemas.PaymentResponse(
        status=authorization.status.value,
        customer_id=outcome.customer_id,
        transaction_id=authorization.transaction_id,
        amount_minor=authorization.amount_minor,
        currency=authorization.currency,
        card_display=outcome.card_display,
        scheme=authorization.scheme.value,
        authorization_code=authorization.authorization_code,
        decline_reason=(
            authorization.decline_reason.value if authorization.decline_reason else None
        ),
        match_distance=round(outcome.distance, 5),
        match_margin=(
            round(outcome.margin, 5) if outcome.margin != float("inf") else None
        ),
        candidates_considered=outcome.candidates_considered,
        sca=schemas.SCAModel(
            strongly_authenticated=outcome.sca.strongly_authenticated,
            categories=[c.value for c in outcome.sca.categories],
            exemption=outcome.sca.exemption.value,
            may_proceed=outcome.sca.may_proceed,
            reasons=list(outcome.sca.reasons),
        ),
    )


@app.get(
    "/v1/customers/{customer_id}",
    response_model=schemas.CustomerResponse,
)
async def get_customer(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
) -> schemas.CustomerResponse:
    authorize_self(caller, customer_id)
    services = container()
    profile = services.repository.get_profile(customer_id)
    if profile is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "unknown customer"}
        )

    card_display: str | None = None
    if profile.is_active:
        try:
            card_display = services.payment.load_card_token(profile).display()
        except (PaymentDeclined, DecryptionError):
            card_display = None

    consents = services.repository.get_consents(customer_id)
    return schemas.CustomerResponse(
        customer_id=profile.customer_id,
        status=profile.status.value,
        engine_id=profile.engine_id,
        card_display=card_display,
        created_at=profile.created_at.isoformat(),
        consent_active=any(c.is_active for c in consents),
    )


@app.delete(
    "/v1/customers/{customer_id}",
    response_model=schemas.ErasureResponse,
)
async def erase_customer(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
) -> schemas.ErasureResponse:
    """GDPR erasure by crypto-shred: destroy the DEK, orphan the ciphertext."""
    authorize_self(caller, customer_id)
    services = container()
    if services.repository.get_profile(customer_id) is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "unknown customer"}
        )

    erased = services.enrollment.delete_customer(customer_id)
    return schemas.ErasureResponse(
        customer_id=customer_id,
        erased=erased,
        detail=(
            "Data encryption key destroyed. All ciphertext for this customer is "
            "permanently unrecoverable, including in existing backups. Proof of "
            "consent is retained as the legal record that processing was lawful."
            if erased
            else "Customer was already erased."
        ),
    )


@app.post(
    "/v1/customers/{customer_id}/palms",
    response_model=schemas.PalmsResponse,
)
async def add_palm(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
    frames: Annotated[list[UploadFile], File(description="Frames of the additional hand")],
    label: Annotated[str, Form()] = "secondary",
) -> schemas.PalmsResponse:
    """Enrol an additional hand (PD-21).

    A customer with only one enrolled palm cannot pay with a bandaged or
    injured hand. Consent is not re-collected: the existing grant already
    covers biometric processing for this customer and this adds no new purpose.
    """
    authorize_self(caller, customer_id)
    services = container()

    if len(frames) > MAX_FRAMES:
        raise HTTPException(
            status_code=400,
            detail={"code": "too_many_frames", "message": f"at most {MAX_FRAMES} frames"},
        )

    images = [
        decode_image(await item.read(), f"frame {index}")
        for index, item in enumerate(frames, 1)
    ]

    try:
        total = services.enrollment.add_palm(
            customer_id=customer_id, frames=images, label=label
        )
    except EnrollmentError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "not_found" else 422,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.PalmsResponse(
        customer_id=customer_id, palms_enrolled=total, label=label
    )


@app.post(
    "/v1/customers/{customer_id}/card",
    response_model=schemas.CardResponse,
)
async def replace_card(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
    card_number: Annotated[str, Form()],
    card_exp_month: Annotated[int, Form()],
    card_exp_year: Annotated[int, Form()],
    card_cvv: Annotated[str, Form()],
    card_holder: Annotated[str, Form()] = "",
) -> schemas.CardResponse:
    """Replace the one card on file, without re-scanning the palm (PD-28).

    Cards expire and get reissued far more often than palms change. Tying the
    two together would force a biometric re-enrollment for what is a purely
    financial event.
    """
    authorize_self(caller, customer_id)
    services = container()

    try:
        card = CardDetails(
            pan=card_number,
            exp_month=card_exp_month,
            exp_year=card_exp_year,
            cvv=card_cvv,
            holder_name=card_holder,
        )
    except PaymentError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_card", "message": str(exc)}
        ) from exc

    try:
        token = services.account.replace_card(customer_id, card)
    except AccountError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "not_found" else 422,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.CardResponse(
        customer_id=customer_id,
        card_display=token.display(),
        scheme=token.scheme.value,
    )


@app.post(
    "/v1/customers/{customer_id}/consent/withdraw",
    response_model=schemas.ConsentStateResponse,
)
async def withdraw_consent(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
) -> schemas.ConsentStateResponse:
    """Stop biometric processing while keeping the data (PD-22).

    Distinct from erasure, because withdrawing consent and destroying data are
    different rights and a customer may want the first without the second. The
    palm stops working immediately; the record survives so the decision can be
    reversed.
    """
    authorize_self(caller, customer_id)
    services = container()
    try:
        services.account.withdraw_consent(customer_id)
    except AccountError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "not_found" else 409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    return schemas.ConsentStateResponse(
        customer_id=customer_id,
        profile_status="suspended",
        consent_active=False,
        detail=(
            "Biometric processing has stopped and this palm can no longer pay. "
            "The data is retained so consent can be restored or the data "
            "exported; erase the profile to destroy it permanently."
        ),
    )


@app.post(
    "/v1/customers/{customer_id}/consent/restore",
    response_model=schemas.ConsentStateResponse,
)
async def restore_consent(
    customer_id: str,
    caller: Annotated[str, Depends(require_customer)],
    consent_purposes: Annotated[str, Form()] = "",
    consent_policy_version: Annotated[str, Form()] = "",
    consent_evidence: Annotated[str, Form()] = "",
) -> schemas.ConsentStateResponse:
    """Re-consent and reactivate a suspended profile.

    Requires a fresh grant rather than reviving the withdrawn one: consent that
    was withdrawn is spent, and the customer must see the current policy
    version to give it again.
    """
    authorize_self(caller, customer_id)
    services = container()

    grant = ConsentGrant(
        granted=True,
        purposes=tuple(p.strip() for p in consent_purposes.split(",") if p.strip()),
        policy_version=consent_policy_version,
        evidence_text=consent_evidence,
    )
    try:
        services.account.restore_consent(customer_id, grant)
    except AccountError as exc:
        raise HTTPException(
            status_code=404 if exc.code == "not_found" else 409,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.ConsentStateResponse(
        customer_id=customer_id,
        profile_status="active",
        consent_active=True,
        detail="Consent restored. This palm can pay again.",
    )


@app.post(
    "/v1/payments/{transaction_id}/refund",
    response_model=schemas.RefundResponse,
    dependencies=[Depends(require_api_key)],
)
async def refund_payment(
    transaction_id: str,
    merchant_id: Annotated[str, Form()],
    amount_minor: Annotated[int | None, Form()] = None,
) -> schemas.RefundResponse:
    """Refund a charge, whole or partial (PD-16).

    Authorised against our own payment record, so a terminal can only reverse
    charges taken by its own merchant.
    """
    services = container()
    try:
        result = services.payment.refund(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            amount_minor=amount_minor,
        )
    except PaymentDeclined as exc:
        raise HTTPException(
            status_code=404 if exc.code == "unknown_transaction" else 422,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.RefundResponse(
        status=result.status.value,
        refund_id=result.transaction_id,
        transaction_id=transaction_id,
        amount_minor=result.amount_minor,
        currency=result.currency,
    )


@app.post(
    "/v1/payments/{transaction_id}/void",
    response_model=schemas.RefundResponse,
    dependencies=[Depends(require_api_key)],
)
async def void_payment(
    transaction_id: str,
    merchant_id: Annotated[str, Form()],
) -> schemas.RefundResponse:
    """Cancel an uncaptured authorisation (PD-16)."""
    services = container()
    try:
        result = services.payment.void(
            transaction_id=transaction_id, merchant_id=merchant_id
        )
    except PaymentDeclined as exc:
        raise HTTPException(
            status_code=404 if exc.code == "unknown_transaction" else 422,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.RefundResponse(
        status=result.status.value,
        refund_id=result.transaction_id,
        transaction_id=transaction_id,
        amount_minor=result.amount_minor,
        currency=result.currency,
    )


@app.get(
    "/v1/audit",
    response_model=list[schemas.AuditEntry],
    dependencies=[Depends(require_admin_key)],
)
async def audit(limit: int = 50) -> list[schemas.AuditEntry]:
    services = container()
    return [
        schemas.AuditEntry(
            event_id=event.event_id,
            event_type=event.event_type,
            customer_id=event.customer_id,
            merchant_id=event.merchant_id,
            outcome=event.outcome,
            detail=event.detail,
            created_at=event.created_at.isoformat(),
        )
        for event in services.repository.recent_audit(min(limit, 500))
    ]


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException) -> JSONResponse:
    """Return errors in one consistent shape the Android client can parse."""
    detail = exc.detail
    if isinstance(detail, dict):
        body = {
            "code": detail.get("code", "error"),
            "message": detail.get("message", ""),
            "detail": detail.get("detail", {}),
        }
    else:
        body = {"code": "error", "message": str(detail), "detail": {}}
    return JSONResponse(
        status_code=exc.status_code, content=body, headers=exc.headers
    )
