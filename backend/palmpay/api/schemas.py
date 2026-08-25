"""Request and response bodies for the HTTP API.

Response shapes are the contract the Android client codes against, so they are
explicit rather than free-form dicts. Nothing here ever carries a template, a
raw image, a PAN or a decrypted DEK.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    engine_id: str
    modality: str
    enrolled_profiles: int
    match_threshold: float
    liveness_required: bool
    enrollment_samples: int
    gateway: str


class QualityModel(BaseModel):
    ok: bool
    sharpness: float
    exposure: float
    contrast: float
    coverage: float
    reasons: list[str] = Field(default_factory=list)


class LivenessModel(BaseModel):
    passed: bool
    high_freq_ratio: float
    specular_fraction: float
    moire_peak: float
    chroma_std: float
    reasons: list[str] = Field(default_factory=list)


class CaptureCheckResponse(BaseModel):
    """Pre-flight feedback so the client can coach the user while framing.

    Deliberately does not create a template or touch the database: it is safe
    to call repeatedly from a camera preview loop.
    """

    usable: bool
    palm_found: bool
    quality: QualityModel | None = None
    liveness: LivenessModel | None = None
    guidance: list[str] = Field(default_factory=list)


class EnrollmentResponse(BaseModel):
    customer_id: str
    engine_id: str
    card_display: str
    card_scheme: str
    hint_type: str
    sample_count: int
    max_pairwise_distance: float
    quality: list[QualityModel] = Field(default_factory=list)


class SCAModel(BaseModel):
    strongly_authenticated: bool
    categories: list[str]
    exemption: str
    may_proceed: bool
    reasons: list[str]


class PaymentResponse(BaseModel):
    status: str
    customer_id: str
    transaction_id: str
    amount_minor: int
    currency: str
    card_display: str
    scheme: str
    authorization_code: str = ""
    decline_reason: str | None = None
    match_distance: float
    match_margin: float | None = None
    candidates_considered: int
    sca: SCAModel


class CustomerResponse(BaseModel):
    customer_id: str
    status: str
    engine_id: str
    card_display: str | None = None
    created_at: str
    consent_active: bool


class ErasureResponse(BaseModel):
    customer_id: str
    erased: bool
    method: str = "crypto_shred"
    detail: str


class AuditEntry(BaseModel):
    event_id: str
    event_type: str
    customer_id: str | None
    merchant_id: str | None
    outcome: str
    detail: dict
    created_at: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict = Field(default_factory=dict)
