"""Card handling, the mocked gateway, and PSD2 SCA policy."""

from __future__ import annotations

import pytest

from palmpay.payments.gateway import (
    AuthorizationRequest,
    CardDetails,
    CardScheme,
    DeclineReason,
    PaymentError,
    detect_scheme,
    luhn_valid,
)
from palmpay.payments.nexi_mock import TEST_CARDS, MockNexiGateway
from palmpay.payments.sca import (
    Exemption,
    FactorCategory,
    HintType,
    LowValueTracker,
    assess,
    hint_factor,
    palm_factor,
)


def approved_sca():
    return assess(
        [palm_factor(0.12, 0.34, True), hint_factor(HintType.SECRET)],
        customer_id="cus_1",
        amount_minor=1000,
    )


class TestCardPrimitives:
    @pytest.mark.parametrize("name", list(TEST_CARDS))
    def test_all_test_cards_are_luhn_valid(self, name):
        assert luhn_valid(TEST_CARDS[name])

    def test_rejects_luhn_failure(self):
        assert not luhn_valid("4111111111111112")

    @pytest.mark.parametrize(
        ("pan", "scheme"),
        [
            ("4111111111111111", CardScheme.VISA),
            ("5555555555554444", CardScheme.MASTERCARD),
            # The 2-series range added in 2017. Systems that only check 51-55
            # silently reject a growing share of real Mastercards.
            ("2223003122003222", CardScheme.MASTERCARD),
            ("6011111111111117", CardScheme.UNKNOWN),
        ],
    )
    def test_scheme_detection(self, pan, scheme):
        assert detect_scheme(pan) is scheme

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(PaymentError, match="Visa and Mastercard"):
            CardDetails("6011111111111117", 12, 2032, "123")

    def test_rejects_invalid_expiry_month(self):
        with pytest.raises(PaymentError, match="expiry month"):
            CardDetails(TEST_CARDS["visa_ok"], 13, 2032, "123")

    def test_repr_never_leaks_the_pan(self):
        """A PAN reaching a log line or a traceback frame is a PCI incident."""
        card = CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123")
        assert TEST_CARDS["visa_ok"] not in repr(card)
        assert "****1111" in repr(card)

    def test_detects_expired_card(self):
        assert CardDetails(TEST_CARDS["visa_ok"], 1, 2020, "123").is_expired()


class TestGateway:
    def test_tokenise_returns_scheme_metadata(self):
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")
        assert token.scheme is CardScheme.VISA
        assert token.last4 == "1111"
        assert token.scheme_reference, "stored-credential reference is required"

    def test_token_does_not_contain_the_pan(self):
        gateway = MockNexiGateway()
        pan = TEST_CARDS["visa_ok"]
        token = gateway.tokenize(CardDetails(pan, 12, 2032, "123"), "cus_1")
        assert pan not in token.token

    def test_visa_and_mastercard_take_the_same_path(self):
        """Nexi acquires both, so scheme must be metadata, not a branch."""
        gateway = MockNexiGateway()
        results = []
        for name in ("visa_ok", "mastercard_ok", "mastercard_2series_ok"):
            token = gateway.tokenize(CardDetails(TEST_CARDS[name], 12, 2032, "123"), "cus_1")
            results.append(
                gateway.authorize(
                    AuthorizationRequest(
                        amount_minor=1500,
                        currency="EUR",
                        merchant_id="mrc_1",
                        token=token,
                        customer_id="cus_1",
                        sca=approved_sca(),
                        idempotency_key=f"idem_{name}",
                    )
                )
            )
        assert all(r.approved for r in results)

    def test_declines_when_sca_not_satisfied(self):
        """The gateway is the last line that can stop an under-authenticated charge."""
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")
        # Inherence only, above the low-value ceiling: not SCA, no exemption.
        weak = assess(
            [palm_factor(0.12, 0.34, True)], customer_id="cus_1", amount_minor=50_000
        )
        result = gateway.authorize(
            AuthorizationRequest(
                amount_minor=50_000,
                currency="EUR",
                merchant_id="mrc_1",
                token=token,
                customer_id="cus_1",
                sca=weak,
                idempotency_key="idem_weak",
            )
        )
        assert not result.approved
        assert result.decline_reason is DeclineReason.SCA_REQUIRED

    def test_declines_on_insufficient_funds(self):
        gateway = MockNexiGateway()
        token = gateway.tokenize(
            CardDetails(TEST_CARDS["visa_insufficient_funds"], 12, 2032, "123"), "cus_1"
        )
        result = gateway.authorize(
            AuthorizationRequest(
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
                token=token,
                customer_id="cus_1",
                sca=approved_sca(),
                idempotency_key="idem_nsf",
            )
        )
        assert result.decline_reason is DeclineReason.INSUFFICIENT_FUNDS

    def test_idempotency_prevents_double_charge(self):
        """A dropped response then a retry must not charge twice."""
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")

        def charge():
            return gateway.authorize(
                AuthorizationRequest(
                    amount_minor=2500,
                    currency="EUR",
                    merchant_id="mrc_1",
                    token=token,
                    customer_id="cus_1",
                    sca=approved_sca(),
                    idempotency_key="idem_same",
                )
            )

        before = gateway.balance_of(token.token)
        first, second = charge(), charge()
        assert first.transaction_id == second.transaction_id
        assert gateway.balance_of(token.token) == before - 2500

    def test_refund_returns_funds(self):
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")
        before = gateway.balance_of(token.token)
        auth = gateway.authorize(
            AuthorizationRequest(
                amount_minor=3000,
                currency="EUR",
                merchant_id="mrc_1",
                token=token,
                customer_id="cus_1",
                sca=approved_sca(),
                idempotency_key="idem_refund",
            )
        )
        gateway.refund(auth.transaction_id)
        assert gateway.balance_of(token.token) == before

    def test_cannot_refund_more_than_charged(self):
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")
        auth = gateway.authorize(
            AuthorizationRequest(
                amount_minor=3000,
                currency="EUR",
                merchant_id="mrc_1",
                token=token,
                customer_id="cus_1",
                sca=approved_sca(),
                idempotency_key="idem_over",
            )
        )
        gateway.refund(auth.transaction_id, 2000)
        with pytest.raises(PaymentError, match="remaining refundable"):
            gateway.refund(auth.transaction_id, 2000)

    def test_rejects_non_positive_amount(self):
        gateway = MockNexiGateway()
        token = gateway.tokenize(CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"), "cus_1")
        with pytest.raises(PaymentError, match="positive"):
            AuthorizationRequest(
                amount_minor=0,
                currency="EUR",
                merchant_id="mrc_1",
                token=token,
                customer_id="cus_1",
                sca=approved_sca(),
                idempotency_key="idem_zero",
            )


class TestSCA:
    def test_palm_plus_secret_hint_is_strong_authentication(self):
        """Two categories -- inherence and knowledge -- so any amount is allowed."""
        result = assess(
            [palm_factor(0.10, 0.34, True), hint_factor(HintType.SECRET)],
            customer_id="cus_1",
            amount_minor=500_000,
        )
        assert result.strongly_authenticated
        assert set(result.categories) == {FactorCategory.INHERENCE, FactorCategory.KNOWLEDGE}
        assert result.may_proceed

    def test_public_hint_contributes_no_factor(self):
        """A phone last-4 is an identifier, not a secret. It proves nothing."""
        assert hint_factor(HintType.PUBLIC) is None

    def test_palm_alone_is_not_sca(self):
        """The design doc's claim that the palm alone replaces the PIN.

        PSD2 requires two categories; a palm is one. Large amounts must fail.
        """
        result = assess(
            [palm_factor(0.10, 0.34, True)], customer_id="cus_1", amount_minor=500_000
        )
        assert not result.strongly_authenticated
        assert not result.may_proceed
        assert "sca_required_but_not_satisfied" in result.reasons

    def test_palm_alone_allowed_under_low_value_exemption(self):
        result = assess(
            [palm_factor(0.10, 0.34, True)],
            customer_id="cus_1",
            amount_minor=1500,
            tracker=LowValueTracker(),
        )
        assert result.may_proceed
        assert result.exemption is Exemption.LOW_VALUE

    def test_low_value_exemption_exhausts(self):
        """PSD2 caps the exemption at 5 transactions or EUR 150 cumulative."""
        tracker = LowValueTracker()
        for _ in range(5):
            result = assess(
                [palm_factor(0.10, 0.34, True)],
                customer_id="cus_1",
                amount_minor=1000,
                tracker=tracker,
            )
            assert result.may_proceed
            tracker.record_exempt("cus_1", 1000)

        exhausted = assess(
            [palm_factor(0.10, 0.34, True)],
            customer_id="cus_1",
            amount_minor=1000,
            tracker=tracker,
        )
        assert not exhausted.may_proceed

    def test_low_value_exemption_respects_cumulative_cap(self):
        tracker = LowValueTracker()
        tracker.record_exempt("cus_1", 14_500)
        result = assess(
            [palm_factor(0.10, 0.34, True)],
            customer_id="cus_1",
            amount_minor=1000,
            tracker=tracker,
        )
        assert not result.may_proceed

    def test_failed_liveness_voids_the_inherence_factor(self):
        """A spoofed palm is not an authentication element at all."""
        result = assess(
            [palm_factor(0.10, 0.34, liveness_passed=False), hint_factor(HintType.SECRET)],
            customer_id="cus_1",
            amount_minor=1000,
        )
        assert not result.strongly_authenticated
        assert FactorCategory.INHERENCE not in result.categories
        assert "inherence_rejected_liveness_failed" in result.reasons
