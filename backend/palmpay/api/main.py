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
from ..services.container import ServiceContainer
from ..services.enrollment import ConsentGrant, EnrollmentError
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
    """Reject unauthenticated callers when an API key is configured.

    Open by default so the prototype runs with no setup. Set ``PALMPAY_API_KEY``
    and it is enforced -- which any deployment reachable by anything other than
    localhost must do.
    """
    import os

    expected = os.environ.get("PALMPAY_API_KEY")
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "invalid API key"})


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
    dependencies=[Depends(require_api_key)],
)
async def capture_check(
    image: Annotated[UploadFile, File(description="A single palm frame")],
) -> schemas.CaptureCheckResponse:
    """Score a frame without enrolling or charging anything."""
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
    dependencies=[Depends(require_api_key)],
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
) -> schemas.EnrollmentResponse:
    """One-time enrollment: link a palm to a tokenised card.

    The card fields are accepted here only because this is a prototype. In
    production the PAN must be collected by a gateway-hosted field so it never
    reaches this service, which is what keeps PCI DSS scope small.
    """
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
        )
    except EnrollmentError as exc:
        status = 403 if exc.code.startswith("consent") else 422
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "detail": exc.detail},
        ) from exc

    return schemas.EnrollmentResponse(
        customer_id=result.customer_id,
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
    dependencies=[Depends(require_api_key)],
)
async def get_customer(customer_id: str) -> schemas.CustomerResponse:
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
    dependencies=[Depends(require_api_key)],
)
async def erase_customer(customer_id: str) -> schemas.ErasureResponse:
    """GDPR erasure by crypto-shred: destroy the DEK, orphan the ciphertext."""
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


@app.get(
    "/v1/audit",
    response_model=list[schemas.AuditEntry],
    dependencies=[Depends(require_api_key)],
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
    return JSONResponse(status_code=exc.status_code, content=body)
