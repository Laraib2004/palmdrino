"""Payment gateway interface and card primitives.

One gateway abstraction handles every scheme. Nexi acquires Visa and Mastercard
through the same tokenisation and authorisation path, so scheme is metadata --
used for display and for scheme-specific rules like Visa Stored Credential
Transaction identifiers -- and never a branch in the payment flow.

Money is integer minor units (euro cents) throughout. Floats are not
acceptable for currency: 0.1 + 0.2 != 0.3 in binary floating point, and a
rounding drift in an authorisation amount is a reconciliation incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from ..store.models import utc_now
from .sca import SCAAssessment


class CardScheme(str, Enum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    UNKNOWN = "unknown"


class AuthorizationStatus(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    ERROR = "error"


class DeclineReason(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    CARD_BLOCKED = "card_blocked"
    SCA_REQUIRED = "sca_required"
    INVALID_TOKEN = "invalid_token"
    LIMIT_EXCEEDED = "limit_exceeded"
    ISSUER_UNAVAILABLE = "issuer_unavailable"


class PaymentError(Exception):
    """Gateway-level failure (bad request, unknown token, transport problem)."""


def luhn_valid(pan: str) -> bool:
    """Standard mod-10 check. Catches typos, not fraud."""
    digits = [int(ch) for ch in pan if ch.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def detect_scheme(pan: str) -> CardScheme:
    """Identify the card scheme from its IIN range.

    Mastercard covers the legacy 51-55 range plus the 2221-2720 range added in
    2017; systems that only check 51-55 silently reject a growing share of real
    Mastercards.
    """
    digits = "".join(ch for ch in pan if ch.isdigit())
    if not digits:
        return CardScheme.UNKNOWN
    if digits[0] == "4":
        return CardScheme.VISA
    if len(digits) >= 2 and 51 <= int(digits[:2]) <= 55:
        return CardScheme.MASTERCARD
    if len(digits) >= 4 and 2221 <= int(digits[:4]) <= 2720:
        return CardScheme.MASTERCARD
    return CardScheme.UNKNOWN


@dataclass
class CardDetails:
    """Raw card data. Transient -- never persisted, never logged.

    Exists only between the enrollment request and the tokenise call. In a real
    deployment even this is avoided: the card is entered into a gateway-hosted
    field so the PAN never touches this service at all, which is what keeps it
    out of PCI DSS scope (design section 6.6).
    """

    pan: str
    exp_month: int
    exp_year: int
    cvv: str
    holder_name: str = ""

    def __post_init__(self) -> None:
        if not luhn_valid(self.pan):
            raise PaymentError("card number failed the Luhn check")
        if not 1 <= self.exp_month <= 12:
            raise PaymentError("invalid expiry month")
        if detect_scheme(self.pan) is CardScheme.UNKNOWN:
            raise PaymentError("only Visa and Mastercard are supported")

    @property
    def scheme(self) -> CardScheme:
        return detect_scheme(self.pan)

    @property
    def last4(self) -> str:
        return "".join(ch for ch in self.pan if ch.isdigit())[-4:]

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or utc_now()
        if self.exp_year < moment.year:
            return True
        return self.exp_year == moment.year and self.exp_month < moment.month

    def redacted(self) -> str:
        return f"{self.scheme.value}:****{self.last4}"

    def __repr__(self) -> str:
        # Guards against a PAN reaching a log line through an f-string or a
        # traceback frame dump.
        return f"CardDetails({self.redacted()})"


@dataclass(frozen=True)
class CardToken:
    """A card-on-file token. This is what gets stored, encrypted under the DEK.

    ``scheme_reference`` is the scheme-assigned identifier for the initial
    transaction that established the stored credential. Visa and Mastercard
    both require subsequent merchant-initiated charges to quote it; omitting it
    is a common cause of unexplained declines on recurring card-on-file flows.
    """

    token: str
    scheme: CardScheme
    last4: str
    exp_month: int
    exp_year: int
    scheme_reference: str = ""
    created_at: datetime = field(default_factory=utc_now)

    def display(self) -> str:
        return f"{self.scheme.value.title()} ****{self.last4}"

    def to_payload(self) -> dict:
        """The form stored, encrypted, in a customer profile."""
        return {
            "token": self.token,
            "scheme": self.scheme.value,
            "last4": self.last4,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "scheme_reference": self.scheme_reference,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "CardToken":
        return cls(
            token=payload["token"],
            scheme=CardScheme(payload["scheme"]),
            last4=payload["last4"],
            exp_month=int(payload["exp_month"]),
            exp_year=int(payload["exp_year"]),
            scheme_reference=payload.get("scheme_reference", ""),
        )


@dataclass(frozen=True)
class AuthorizationRequest:
    amount_minor: int
    currency: str
    merchant_id: str
    token: CardToken
    customer_id: str
    sca: SCAAssessment
    idempotency_key: str
    description: str = ""

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise PaymentError("amount must be positive")
        if len(self.currency) != 3:
            raise PaymentError("currency must be an ISO 4217 alpha-3 code")


@dataclass(frozen=True)
class AuthorizationResult:
    status: AuthorizationStatus
    transaction_id: str
    amount_minor: int
    currency: str
    scheme: CardScheme
    decline_reason: DeclineReason | None = None
    authorization_code: str = ""
    captured: bool = False
    created_at: datetime = field(default_factory=utc_now)

    @property
    def approved(self) -> bool:
        return self.status is AuthorizationStatus.APPROVED

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "transaction_id": self.transaction_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "scheme": self.scheme.value,
            "decline_reason": self.decline_reason.value if self.decline_reason else None,
            "authorization_code": self.authorization_code,
            "captured": self.captured,
            "created_at": self.created_at.isoformat(),
        }


class PaymentGateway(Protocol):
    """What a PSP integration must provide.

    The real Nexi adapter implements exactly this. Because the mock does too,
    every layer above -- identification, the HTTP API, the Android client --
    is written and tested once and does not change when the real credentials
    arrive.
    """

    name: str

    def tokenize(self, card: CardDetails, customer_id: str) -> CardToken: ...

    def authorize(self, request: AuthorizationRequest) -> AuthorizationResult: ...

    def capture(self, transaction_id: str, amount_minor: int | None = None) -> AuthorizationResult: ...

    def refund(self, transaction_id: str, amount_minor: int | None = None) -> AuthorizationResult: ...

    def void(self, transaction_id: str) -> AuthorizationResult: ...
