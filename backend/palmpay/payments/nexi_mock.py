"""Mocked Nexi gateway.

Simulates the acquirer end of the flow with dummy balances so the full
enrollment -> identify -> charge path runs end to end without credentials and
without money moving. It implements ``PaymentGateway``, so replacing it with a
real Nexi adapter is a one-line swap in the service wiring.

What a real adapter has to add (deliberately not guessed at here -- endpoint
shapes must come from Nexi's own documentation, not from invention):

* HTTPS calls to the Nexi XPay / card-on-file endpoints with merchant
  credentials and request signing;
* the tokenisation call, which must be a *customer-initiated* transaction with
  the cardholder present, since that is what establishes the stored credential
  the later palm-triggered charges rely on;
* propagation of the scheme reference on every merchant-initiated charge;
* their asynchronous notification/webhook handling for captures and refunds;
* retry and reconciliation for the case where the network drops after the
  acquirer authorised but before the response arrived.

Behaviour here is intentionally deterministic so tests can assert on declines.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field

from ..store.models import utc_now
from .gateway import (
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
    CardDetails,
    CardScheme,
    CardToken,
    DeclineReason,
    PaymentError,
)

# Cards whose last four digits trigger a specific decline, for testing the
# unhappy paths. Everything else behaves as a normal funded card.
SCRIPTED_DECLINES: dict[str, DeclineReason] = {
    "0002": DeclineReason.INSUFFICIENT_FUNDS,
    "0069": DeclineReason.CARD_BLOCKED,
    "0119": DeclineReason.ISSUER_UNAVAILABLE,
}

DEFAULT_BALANCE_MINOR = 100_000  # EUR 1,000.00


@dataclass
class _Account:
    token: CardToken
    balance_minor: int
    blocked: bool = False


@dataclass
class _Transaction:
    result: AuthorizationResult
    token: str
    refunded_minor: int = 0


@dataclass
class MockNexiGateway:
    """In-memory acquirer simulation. Implements ``PaymentGateway``."""

    name: str = "nexi-mock"
    default_balance_minor: int = DEFAULT_BALANCE_MINOR
    _accounts: dict[str, _Account] = field(default_factory=dict)
    _transactions: dict[str, _Transaction] = field(default_factory=dict)
    _idempotency: dict[str, AuthorizationResult] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- tokenisation ---------------------------------------------------------

    def tokenize(self, card: CardDetails, customer_id: str) -> CardToken:
        """Exchange card details for a storable token.

        The PAN is used to derive the token's public metadata and is then
        dropped: nothing in this object retains it, mirroring the property the
        real integration must have to stay out of PCI DSS scope.
        """
        if card.is_expired():
            raise PaymentError("card is expired")

        token = CardToken(
            token=f"tok_{secrets.token_hex(12)}",
            scheme=card.scheme,
            last4=card.last4,
            exp_month=card.exp_month,
            exp_year=card.exp_year,
            # Stands in for the scheme-assigned stored-credential identifier
            # returned by the initial cardholder-present transaction.
            scheme_reference=f"scr_{secrets.token_hex(8)}",
        )

        blocked = SCRIPTED_DECLINES.get(card.last4) is DeclineReason.CARD_BLOCKED
        with self._lock:
            self._accounts[token.token] = _Account(
                token=token,
                balance_minor=self.default_balance_minor,
                blocked=blocked,
            )
        return token

    # -- authorisation --------------------------------------------------------

    def _decline(
        self, request: AuthorizationRequest, reason: DeclineReason
    ) -> AuthorizationResult:
        return AuthorizationResult(
            status=AuthorizationStatus.DECLINED,
            transaction_id=f"txn_{secrets.token_hex(10)}",
            amount_minor=request.amount_minor,
            currency=request.currency,
            scheme=request.token.scheme,
            decline_reason=reason,
        )

    def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        with self._lock:
            # Idempotency: a retried request after a dropped response must not
            # charge the customer twice. Real acquirers require this and so
            # does the terminal flow, where a lost reply is routine.
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                return cached

            result = self._authorize_locked(request)
            self._idempotency[request.idempotency_key] = result
            return result

    def _authorize_locked(self, request: AuthorizationRequest) -> AuthorizationResult:
        # The gateway is the last line that can stop an under-authenticated
        # charge, so the SCA verdict is enforced here rather than trusted from
        # the caller.
        if not request.sca.may_proceed:
            return self._decline(request, DeclineReason.SCA_REQUIRED)

        account = self._accounts.get(request.token.token)
        if account is None:
            return self._decline(request, DeclineReason.INVALID_TOKEN)
        if account.blocked:
            return self._decline(request, DeclineReason.CARD_BLOCKED)

        scripted = SCRIPTED_DECLINES.get(account.token.last4)
        if scripted is DeclineReason.ISSUER_UNAVAILABLE:
            return self._decline(request, scripted)
        if scripted is DeclineReason.INSUFFICIENT_FUNDS:
            return self._decline(request, scripted)

        now = utc_now()
        expired = account.token.exp_year < now.year or (
            account.token.exp_year == now.year and account.token.exp_month < now.month
        )
        if expired:
            return self._decline(request, DeclineReason.CARD_EXPIRED)

        if account.balance_minor < request.amount_minor:
            return self._decline(request, DeclineReason.INSUFFICIENT_FUNDS)

        account.balance_minor -= request.amount_minor
        result = AuthorizationResult(
            status=AuthorizationStatus.APPROVED,
            transaction_id=f"txn_{secrets.token_hex(10)}",
            amount_minor=request.amount_minor,
            currency=request.currency,
            scheme=request.token.scheme,
            authorization_code=secrets.token_hex(3).upper(),
        )
        self._transactions[result.transaction_id] = _Transaction(
            result=result, token=request.token.token
        )
        return result

    # -- post-authorisation ---------------------------------------------------

    def _lookup(self, transaction_id: str) -> _Transaction:
        record = self._transactions.get(transaction_id)
        if record is None:
            raise PaymentError(f"unknown transaction: {transaction_id}")
        return record

    def capture(
        self, transaction_id: str, amount_minor: int | None = None
    ) -> AuthorizationResult:
        with self._lock:
            record = self._lookup(transaction_id)
            if not record.result.approved:
                raise PaymentError("cannot capture a declined authorisation")
            amount = amount_minor or record.result.amount_minor
            if amount > record.result.amount_minor:
                raise PaymentError("capture exceeds the authorised amount")

            captured = AuthorizationResult(
                status=AuthorizationStatus.APPROVED,
                transaction_id=transaction_id,
                amount_minor=amount,
                currency=record.result.currency,
                scheme=record.result.scheme,
                authorization_code=record.result.authorization_code,
                captured=True,
            )
            record.result = captured
            return captured

    def refund(
        self, transaction_id: str, amount_minor: int | None = None
    ) -> AuthorizationResult:
        with self._lock:
            record = self._lookup(transaction_id)
            if not record.result.approved:
                raise PaymentError("cannot refund a declined authorisation")

            amount = amount_minor or record.result.amount_minor
            remaining = record.result.amount_minor - record.refunded_minor
            if amount > remaining:
                raise PaymentError("refund exceeds the remaining refundable amount")

            record.refunded_minor += amount
            account = self._accounts.get(record.token)
            if account is not None:
                account.balance_minor += amount

            return AuthorizationResult(
                status=AuthorizationStatus.APPROVED,
                transaction_id=f"rfnd_{secrets.token_hex(10)}",
                amount_minor=amount,
                currency=record.result.currency,
                scheme=record.result.scheme,
                authorization_code=record.result.authorization_code,
                captured=True,
            )

    def void(self, transaction_id: str) -> AuthorizationResult:
        with self._lock:
            record = self._lookup(transaction_id)
            if record.result.captured:
                raise PaymentError("cannot void a captured transaction; refund instead")

            account = self._accounts.get(record.token)
            if account is not None:
                account.balance_minor += record.result.amount_minor

            voided = AuthorizationResult(
                status=AuthorizationStatus.DECLINED,
                transaction_id=transaction_id,
                amount_minor=0,
                currency=record.result.currency,
                scheme=record.result.scheme,
                decline_reason=None,
            )
            record.result = voided
            return voided

    # -- test helpers ---------------------------------------------------------

    def balance_of(self, token: str) -> int:
        with self._lock:
            account = self._accounts.get(token)
            if account is None:
                raise PaymentError(f"unknown token: {token}")
            return account.balance_minor

    def set_balance(self, token: str, amount_minor: int) -> None:
        with self._lock:
            account = self._accounts.get(token)
            if account is None:
                raise PaymentError(f"unknown token: {token}")
            account.balance_minor = amount_minor


TEST_CARDS: dict[str, str] = {
    "visa_ok": "4111111111111111",
    "mastercard_ok": "5555555555554444",
    "mastercard_2series_ok": "2223003122003222",
    "visa_insufficient_funds": "4000000000000002",
    "visa_blocked": "4000000000000069",
    "visa_issuer_unavailable": "4000000000000119",
}
