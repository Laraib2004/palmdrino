"""Enrollment, identification and payment as a whole."""

from __future__ import annotations

import pytest

from palmpay.payments.gateway import CardDetails, DeclineReason
from palmpay.payments.nexi_mock import TEST_CARDS
from palmpay.payments.sca import FactorCategory, HintType
from palmpay.services.enrollment import ConsentGrant, EnrollmentError
from palmpay.services.payment import PaymentDeclined
from palmpay.store.models import ProfileStatus

from .synthetic import enrollment_samples, flat_print, sample

HINT = "4821"


def enroll(services, consent, card, identity=1, hint=HINT, hint_type=HintType.SECRET):
    return services.enrollment.enroll(
        frames=enrollment_samples(identity, services.settings.enrollment_samples),
        hint=hint,
        hint_type=hint_type,
        card=card,
        pii={"name": f"Person {identity}", "email": f"p{identity}@example.it"},
        consent=consent,
    )


class TestEnrollment:
    def test_enrolls_and_links_a_card(self, services, consent, visa_card):
        result = enroll(services, consent, visa_card)
        assert result.customer_id.startswith("cus_")
        assert result.card_display == "Visa ****1111"
        assert result.card_scheme == "visa"
        assert result.max_pairwise_distance < services.settings.enrollment_consistency_max

    def test_mastercard_enrolls_identically(self, services, consent, mastercard):
        result = enroll(services, consent, mastercard, identity=2)
        assert result.card_display == "Mastercard ****4444"

    def test_refuses_without_consent(self, services, visa_card):
        """Art. 9 basis must exist before any biometric processing happens."""
        refused = ConsentGrant(False, ("biometric_processing",), "v1")
        with pytest.raises(EnrollmentError) as exc:
            enroll(services, refused, visa_card)
        assert exc.value.code == "consent_required"

    def test_refuses_incomplete_consent_purposes(self, services, visa_card):
        partial = ConsentGrant(True, ("biometric_processing",), "v1")
        with pytest.raises(EnrollmentError) as exc:
            enroll(services, partial, visa_card)
        assert exc.value.code == "consent_incomplete"
        assert "payment_execution" in exc.value.detail["missing_purposes"]

    def test_requires_enough_samples(self, services, consent, visa_card):
        with pytest.raises(EnrollmentError) as exc:
            services.enrollment.enroll(
                frames=enrollment_samples(1, 1),
                hint=HINT,
                hint_type=HintType.SECRET,
                card=visa_card,
                pii={},
                consent=consent,
            )
        assert exc.value.code == "insufficient_samples"

    def test_rejects_mismatched_samples(self, services, consent, visa_card):
        """Two different hands must not be averaged into one identity."""
        with pytest.raises(EnrollmentError) as exc:
            services.enrollment.enroll(
                frames=[sample(1, seed=1), sample(2, seed=1), sample(3, seed=1)],
                hint=HINT,
                hint_type=HintType.SECRET,
                card=visa_card,
                pii={},
                consent=consent,
            )
        assert exc.value.code == "inconsistent_samples"

    def test_rejects_spoofed_samples(self, services, consent, visa_card):
        with pytest.raises(EnrollmentError) as exc:
            services.enrollment.enroll(
                frames=[flat_print(1)] * 3,
                hint=HINT,
                hint_type=HintType.SECRET,
                card=visa_card,
                pii={},
                consent=consent,
            )
        assert exc.value.code == "liveness_failed"

    def test_rejects_expired_card_without_creating_a_profile(self, services, consent):
        """A rejected card must leave no half-built profile or orphan key."""
        before = services.repository.count_profiles()
        with pytest.raises(EnrollmentError) as exc:
            enroll(services, consent, CardDetails(TEST_CARDS["visa_ok"], 1, 2020, "123"))
        assert exc.value.code == "card_rejected"
        assert services.repository.count_profiles() == before

    def test_nothing_sensitive_is_stored_in_the_clear(self, services, consent, visa_card):
        result = enroll(services, consent, visa_card)
        profile = services.repository.get_profile(result.customer_id)
        blob = profile.enc_template + profile.enc_payment_token + profile.enc_pii
        assert TEST_CARDS["visa_ok"].encode() not in blob
        assert b"Person 1" not in blob
        assert b"@example.it" not in blob
        # The identifier hint is only ever stored as a keyed hash.
        assert HINT not in profile.shard


class TestIdentificationAndPayment:
    def test_pays_with_a_palm(self, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        outcome = services.payment.pay(
            frame=sample(1, shift=(2, -3), rotation_deg=2.0, brightness=0.97, seed=44),
            hint=HINT,
            amount_minor=24_990,
            currency="EUR",
            merchant_id="mrc_bar_roma",
        )
        assert outcome.authorization.approved
        assert outcome.customer_id == enrolled.customer_id
        assert outcome.card_display == "Visa ****1111"

    def test_picks_the_right_person_out_of_a_shard(self, services, consent, visa_card, mastercard):
        """The whole point of 1:small-N: several palms share the hint."""
        target = enroll(services, consent, visa_card, identity=3)
        for identity in (4, 5, 6, 7):
            enroll(services, consent, mastercard, identity=identity)
        assert services.repository.count_in_shard(
            services.repository.get_profile(target.customer_id).shard
        ) == 5

        outcome = services.payment.pay(
            frame=sample(3, shift=(-2, 2), rotation_deg=-1.5, seed=77),
            hint=HINT,
            amount_minor=1_000,
            currency="EUR",
            merchant_id="mrc_1",
        )
        assert outcome.customer_id == target.customer_id
        assert outcome.candidates_considered == 5
        assert outcome.card_display == "Visa ****1111"

    def test_large_amount_needs_no_pin(self, services, consent, visa_card):
        """The headline UX claim: palm plus secret hint authorises any amount.

        EUR 750 is 15x the PSD2 low-value ceiling, so nothing but genuine
        strong authentication can let this through. (It stays under the mock
        gateway's dummy balance so the test exercises SCA, not funding.)
        """
        enroll(services, consent, visa_card)
        outcome = services.payment.pay(
            frame=sample(1, seed=12),
            hint=HINT,
            amount_minor=75_000,  # EUR 750
            currency="EUR",
            merchant_id="mrc_1",
        )
        assert outcome.authorization.approved
        assert outcome.sca.exemption.value == "none", "must be real SCA, not an exemption"
        assert outcome.sca.strongly_authenticated
        assert set(outcome.sca.categories) == {
            FactorCategory.INHERENCE,
            FactorCategory.KNOWLEDGE,
        }

    def test_public_hint_blocks_a_large_amount(self, services, consent, visa_card):
        """With a public identifier there is only one factor, so SCA fails."""
        enroll(services, consent, visa_card, hint_type=HintType.PUBLIC)
        outcome = services.payment.pay(
            frame=sample(1, seed=12),
            hint=HINT,
            # Same amount the secret-hint test approves, and within the dummy
            # balance, so the only difference between the two outcomes is the
            # authentication basis.
            amount_minor=75_000,
            currency="EUR",
            merchant_id="mrc_1",
        )
        assert not outcome.authorization.approved
        assert outcome.authorization.decline_reason is DeclineReason.SCA_REQUIRED

    def test_public_hint_still_allows_small_amounts(self, services, consent, visa_card):
        enroll(services, consent, visa_card, hint_type=HintType.PUBLIC)
        outcome = services.payment.pay(
            frame=sample(1, seed=12),
            hint=HINT,
            amount_minor=1_200,
            currency="EUR",
            merchant_id="mrc_1",
        )
        assert outcome.authorization.approved
        assert outcome.sca.exemption.value == "low_value"

    def test_unenrolled_palm_is_refused(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        with pytest.raises(PaymentDeclined) as exc:
            services.payment.pay(
                frame=sample(42, seed=3),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        assert exc.value.code == "no_match"

    def test_wrong_hint_is_refused(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        with pytest.raises(PaymentDeclined) as exc:
            services.payment.pay(
                frame=sample(1, seed=3),
                hint="9999",
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        assert exc.value.code == "no_match"

    def test_spoofed_palm_is_refused(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        with pytest.raises(PaymentDeclined) as exc:
            services.payment.pay(
                frame=flat_print(1),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        assert exc.value.code == "liveness_failed"

    def test_shard_overflow_refuses_rather_than_truncating(
        self, services, consent, visa_card
    ):
        """A hint that stopped narrowing is an accuracy failure, not a shrug."""
        services.settings.max_candidates = 2
        for identity in (1, 2, 3):
            enroll(services, consent, visa_card, identity=identity)
        with pytest.raises(PaymentDeclined) as exc:
            services.payment.pay(
                frame=sample(1, seed=3),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        assert exc.value.code == "shard_overflow"

    def test_ambiguous_match_is_refused(self, services, consent, visa_card, mastercard):
        """Two near-identical enrolled palms must not be resolved by a coin flip."""
        enroll(services, consent, visa_card, identity=9)
        # Force ambiguity by demanding an unreachable separation.
        services.settings.match_margin = 0.99
        enroll(services, consent, mastercard, identity=10)

        with pytest.raises(PaymentDeclined) as exc:
            services.payment.pay(
                frame=sample(9, seed=5),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        assert exc.value.code == "ambiguous_match"

    def test_idempotency_key_prevents_double_charge(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        kwargs = dict(
            hint=HINT,
            amount_minor=5_000,
            currency="EUR",
            merchant_id="mrc_1",
            idempotency_key="terminal-42-txn-7",
        )
        first = services.payment.pay(frame=sample(1, seed=21), **kwargs)
        second = services.payment.pay(frame=sample(1, seed=22), **kwargs)
        assert first.authorization.transaction_id == second.authorization.transaction_id


class TestErasure:
    def test_crypto_shred_makes_the_palm_unchargeable(self, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        assert services.enrollment.delete_customer(enrolled.customer_id)

        with pytest.raises(PaymentDeclined):
            services.payment.pay(
                frame=sample(1, seed=31),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )

    def test_crypto_shred_destroys_key_and_ciphertext(self, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        services.enrollment.delete_customer(enrolled.customer_id)

        profile = services.repository.get_profile(enrolled.customer_id)
        assert profile.status is ProfileStatus.SHREDDED
        assert profile.wrapped_dek == b""
        assert profile.enc_template == b""
        assert profile.enc_payment_token == b""

    def test_consent_proof_survives_erasure(self, services, consent, visa_card):
        """Deleting the proof would destroy the basis for lawful processing."""
        enrolled = enroll(services, consent, visa_card)
        services.enrollment.delete_customer(enrolled.customer_id)

        consents = services.repository.get_consents(enrolled.customer_id)
        assert len(consents) == 1
        assert consents[0].policy_version == consent.policy_version
        assert not consents[0].is_active, "consent must be marked withdrawn"

    def test_erasure_is_idempotent(self, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        assert services.enrollment.delete_customer(enrolled.customer_id)
        assert not services.enrollment.delete_customer(enrolled.customer_id)


class TestAudit:
    def test_records_enrollment_and_payment(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        services.payment.pay(
            frame=sample(1, seed=8),
            hint=HINT,
            amount_minor=1000,
            currency="EUR",
            merchant_id="mrc_1",
        )
        kinds = {event.event_type for event in services.repository.recent_audit()}
        assert {"enrollment", "payment"} <= kinds

    def test_audit_never_contains_biometric_data_or_pans(
        self, services, consent, visa_card
    ):
        enroll(services, consent, visa_card)
        services.payment.pay(
            frame=sample(1, seed=8),
            hint=HINT,
            amount_minor=1000,
            currency="EUR",
            merchant_id="mrc_1",
        )
        import json

        blob = json.dumps([e.detail for e in services.repository.recent_audit()])
        assert TEST_CARDS["visa_ok"] not in blob
        assert HINT not in blob
        assert "template" not in blob.lower()

    def test_declines_are_audited(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        with pytest.raises(PaymentDeclined):
            services.payment.pay(
                frame=sample(42, seed=3),
                hint=HINT,
                amount_minor=1000,
                currency="EUR",
                merchant_id="mrc_1",
            )
        outcomes = [
            e.outcome for e in services.repository.recent_audit() if e.event_type == "payment"
        ]
        assert "declined" in outcomes
