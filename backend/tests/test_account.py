"""Account management, refunds and maintenance: PD-14, PD-16, PD-22, PD-28."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from palmpay.api.main import app, set_container
from palmpay.crypto.kms import WrappedKey
from palmpay.payments.gateway import CardDetails
from palmpay.payments.nexi_mock import TEST_CARDS
from palmpay.payments.sca import HintType
from palmpay.services.maintenance import KeyMaintenance
from palmpay.store.models import ProfileStatus

from .synthetic import enrollment_samples, sample

HINT = "4821"
PURPOSES = "biometric_processing,payment_execution"


def jpeg(frame: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    return buffer.tobytes()


@pytest.fixture
def client(services):
    set_container(services)
    with TestClient(app) as test_client:
        yield test_client
    set_container(None)


def enroll(services, consent, card, identity=1, hint=HINT):
    return services.enrollment.enroll(
        frames=enrollment_samples(identity, 3),
        hint=hint,
        hint_type=HintType.SECRET,
        card=card,
        pii={"name": f"Person {identity}"},
        consent=consent,
    )


def auth(result) -> dict:
    return {"Authorization": f"Bearer {result.credential}"}


class TestConsentWithdrawal:
    """PD-22: withdrawing consent and erasing data are different rights."""

    def test_withdrawal_stops_the_palm_working(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/consent/withdraw", headers=auth(enrolled)
        )
        assert response.status_code == 200
        assert response.json()["profile_status"] == "suspended"

        paid = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert paid.status_code == 402

    def test_withdrawal_keeps_the_data(self, client, services, consent, visa_card):
        """The distinction from erasure: the record survives."""
        enrolled = enroll(services, consent, visa_card)
        client.post(
            f"/v1/customers/{enrolled.customer_id}/consent/withdraw", headers=auth(enrolled)
        )
        profile = services.repository.get_profile(enrolled.customer_id)
        assert profile.status is ProfileStatus.SUSPENDED
        assert profile.wrapped_dek, "data must survive withdrawal"
        assert services.repository.templates_for(enrolled.customer_id)

    def test_consent_can_be_restored(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        headers = auth(enrolled)
        client.post(f"/v1/customers/{enrolled.customer_id}/consent/withdraw", headers=headers)

        restored = client.post(
            f"/v1/customers/{enrolled.customer_id}/consent/restore",
            headers=headers,
            data={"consent_purposes": PURPOSES, "consent_policy_version": "2026-01-v1"},
        )
        assert restored.status_code == 200

        paid = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert paid.status_code == 200

    def test_restore_requires_complete_consent(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        headers = auth(enrolled)
        client.post(f"/v1/customers/{enrolled.customer_id}/consent/withdraw", headers=headers)

        partial = client.post(
            f"/v1/customers/{enrolled.customer_id}/consent/restore",
            headers=headers,
            data={"consent_purposes": "biometric_processing", "consent_policy_version": "v1"},
        )
        assert partial.status_code == 409
        assert partial.json()["code"] == "consent_incomplete"

    def test_erased_profile_cannot_be_restored(self, client, services, consent, visa_card):
        """No key left, so an account restored here could never be read."""
        enrolled = enroll(services, consent, visa_card)
        headers = auth(enrolled)
        client.delete(f"/v1/customers/{enrolled.customer_id}", headers=headers)
        # The credential is revoked by erasure, so this is a 401 rather than a
        # 409 -- either way the profile cannot come back.
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/consent/restore",
            headers=headers,
            data={"consent_purposes": PURPOSES, "consent_policy_version": "v1"},
        )
        assert response.status_code in (401, 409)

    def test_another_customer_cannot_withdraw_your_consent(
        self, client, services, consent, visa_card, mastercard
    ):
        victim = enroll(services, consent, visa_card, identity=1)
        attacker = enroll(services, consent, mastercard, identity=2)
        response = client.post(
            f"/v1/customers/{victim.customer_id}/consent/withdraw", headers=auth(attacker)
        )
        assert response.status_code == 403


class TestCardReplacement:
    """PD-28: a card change must not require a new palm scan."""

    def test_replaces_the_card(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/card",
            headers=auth(enrolled),
            data={
                "card_number": TEST_CARDS["mastercard_ok"],
                "card_exp_month": 6,
                "card_exp_year": 2033,
                "card_cvv": "456",
            },
        )
        assert response.status_code == 200
        assert response.json()["card_display"] == "Mastercard ****4444"

    def test_palm_still_works_and_charges_the_new_card(
        self, client, services, consent, visa_card
    ):
        enrolled = enroll(services, consent, visa_card)
        client.post(
            f"/v1/customers/{enrolled.customer_id}/card",
            headers=auth(enrolled),
            data={
                "card_number": TEST_CARDS["mastercard_ok"],
                "card_exp_month": 6,
                "card_exp_year": 2033,
                "card_cvv": "456",
            },
        )
        paid = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1500, "merchant_id": "mrc_1"},
        )
        assert paid.status_code == 200
        assert paid.json()["card_display"] == "Mastercard ****4444"

    def test_template_is_untouched(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        before = services.repository.templates_for(enrolled.customer_id)[0].enc_template
        client.post(
            f"/v1/customers/{enrolled.customer_id}/card",
            headers=auth(enrolled),
            data={
                "card_number": TEST_CARDS["mastercard_ok"],
                "card_exp_month": 6,
                "card_exp_year": 2033,
                "card_cvv": "456",
            },
        )
        after = services.repository.templates_for(enrolled.customer_id)[0].enc_template
        assert before == after

    def test_rejects_an_invalid_card(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card)
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/card",
            headers=auth(enrolled),
            data={
                "card_number": "4111111111111112",
                "card_exp_month": 6,
                "card_exp_year": 2033,
                "card_cvv": "456",
            },
        )
        assert response.status_code == 400

    def test_another_customer_cannot_replace_your_card(
        self, client, services, consent, visa_card, mastercard
    ):
        victim = enroll(services, consent, visa_card, identity=1)
        attacker = enroll(services, consent, mastercard, identity=2)
        response = client.post(
            f"/v1/customers/{victim.customer_id}/card",
            headers=auth(attacker),
            data={
                "card_number": TEST_CARDS["mastercard_ok"],
                "card_exp_month": 6,
                "card_exp_year": 2033,
                "card_cvv": "456",
            },
        )
        assert response.status_code == 403


class TestRefundAndVoid:
    """PD-16: a terminal that can charge but not refund is not usable."""

    def _charge(self, client, services, consent, visa_card, merchant="mrc_1"):
        enroll(services, consent, visa_card)
        paid = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 5000, "merchant_id": merchant},
        )
        assert paid.status_code == 200
        return paid.json()["transaction_id"]

    def test_full_refund(self, client, services, consent, visa_card):
        txn = self._charge(client, services, consent, visa_card)
        response = client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1"}
        )
        assert response.status_code == 200
        assert response.json()["amount_minor"] == 5000

    def test_partial_refund_then_remainder(self, client, services, consent, visa_card):
        txn = self._charge(client, services, consent, visa_card)
        first = client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1", "amount_minor": 2000}
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1", "amount_minor": 3000}
        )
        assert second.status_code == 200

    def test_cannot_over_refund(self, client, services, consent, visa_card):
        txn = self._charge(client, services, consent, visa_card)
        client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1", "amount_minor": 4000}
        )
        excess = client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1", "amount_minor": 2000}
        )
        assert excess.status_code == 422
        assert excess.json()["code"] == "refund_exceeds_remaining"

    def test_another_merchant_cannot_refund_your_takings(
        self, client, services, consent, visa_card
    ):
        """Otherwise any terminal could refund any merchant to the cardholder."""
        txn = self._charge(client, services, consent, visa_card, merchant="mrc_1")
        response = client.post(
            f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_other"}
        )
        assert response.status_code == 404
        assert response.json()["code"] == "unknown_transaction"

    def test_unknown_and_foreign_transactions_are_indistinguishable(
        self, client, services, consent, visa_card
    ):
        """So transaction ids cannot be probed from another terminal."""
        txn = self._charge(client, services, consent, visa_card, merchant="mrc_1")
        foreign = client.post(f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_x"})
        invented = client.post("/v1/payments/txn_nope/refund", data={"merchant_id": "mrc_x"})
        assert foreign.status_code == invented.status_code == 404
        assert foreign.json()["code"] == invented.json()["code"]

    def test_void_then_refund_is_refused(self, client, services, consent, visa_card):
        txn = self._charge(client, services, consent, visa_card)
        assert client.post(f"/v1/payments/{txn}/void", data={"merchant_id": "mrc_1"}).status_code == 200
        after = client.post(f"/v1/payments/{txn}/refund", data={"merchant_id": "mrc_1"})
        assert after.status_code == 422


class TestKeyMaintenance:
    """PD-14: rotation is pointless if retired KEKs can never be destroyed."""

    def test_rewrap_moves_deks_to_the_active_kek(self, services, consent, visa_card):
        enroll(services, consent, visa_card, identity=1)
        enroll(services, consent, visa_card, identity=2)
        old = services.kms.active_kek_id
        services.kms.rotate_kek()

        report = services.key_maintenance().rewrap_all()
        assert report.rewrapped == 2
        assert report.unreadable == 0

        for profile in services.repository.iter_profiles():
            assert WrappedKey.deserialize(profile.wrapped_dek).kek_id != old

    def test_old_kek_becomes_destroyable_and_data_survives(
        self, services, consent, visa_card
    ):
        enrolled = enroll(services, consent, visa_card)
        old = services.kms.active_kek_id
        services.kms.rotate_kek()

        report = services.key_maintenance().rewrap_all()
        assert old in report.retired_keks

        services.kms.destroy_kek(old)
        profile = services.repository.get_profile(enrolled.customer_id)
        assert services.payment.load_card_token(profile).display() == "Visa ****1111"

    def test_rewrap_skips_shredded_profiles(self, services, consent, visa_card):
        """Re-creating key material for an erased customer would undo erasure."""
        enrolled = enroll(services, consent, visa_card)
        services.enrollment.delete_customer(enrolled.customer_id)
        services.kms.rotate_kek()

        report = services.key_maintenance().rewrap_all()
        assert report.examined == 0
        assert services.repository.get_profile(enrolled.customer_id).wrapped_dek == b""

    def test_rewrap_is_idempotent(self, services, consent, visa_card):
        enroll(services, consent, visa_card)
        services.kms.rotate_kek()
        services.key_maintenance().rewrap_all()

        again = services.key_maintenance().rewrap_all()
        assert again.rewrapped == 0
        assert again.already_current == 1


class TestMultiplePalms:
    """PD-21: one enrolled hand means an injury stops you paying."""

    def test_second_hand_can_be_enrolled(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card, identity=1)
        files = [
            ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
            # A different synthetic identity stands in for the other hand:
            # two hands of one person are as unalike as two people.
            for i, f in enumerate(enrollment_samples(20, 3), 1)
        ]
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/palms",
            headers=auth(enrolled),
            files=files,
            data={"label": "left"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["palms_enrolled"] == 2

    def test_either_hand_can_pay(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card, identity=1)
        client.post(
            f"/v1/customers/{enrolled.customer_id}/palms",
            headers=auth(enrolled),
            files=[
                ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
                for i, f in enumerate(enrollment_samples(20, 3), 1)
            ],
            data={"label": "left"},
        )

        for identity in (1, 20):
            paid = client.post(
                "/v1/pay",
                files={"image": ("p.jpg", jpeg(sample(identity, seed=44)), "image/jpeg")},
                data={"hint": HINT, "amount_minor": 500, "merchant_id": "mrc_1"},
            )
            assert paid.status_code == 200, f"hand {identity}: {paid.text}"
            assert paid.json()["customer_id"] == enrolled.customer_id

    def test_re_enrolling_the_same_hand_is_refused(
        self, client, services, consent, visa_card
    ):
        """Two near-identical templates would make every payment ambiguous."""
        enrolled = enroll(services, consent, visa_card, identity=1)
        response = client.post(
            f"/v1/customers/{enrolled.customer_id}/palms",
            headers=auth(enrolled),
            files=[
                ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
                for i, f in enumerate(enrollment_samples(1, 3), 1)
            ],
            data={"label": "left"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "same_palm"

    def test_another_customer_cannot_add_a_palm_to_your_account(
        self, client, services, consent, visa_card, mastercard
    ):
        victim = enroll(services, consent, visa_card, identity=1)
        attacker = enroll(services, consent, mastercard, identity=2)
        response = client.post(
            f"/v1/customers/{victim.customer_id}/palms",
            headers=auth(attacker),
            files=[
                ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
                for i, f in enumerate(enrollment_samples(20, 3), 1)
            ],
            data={"label": "left"},
        )
        assert response.status_code == 403

    def test_erasure_removes_every_palm(self, client, services, consent, visa_card):
        enrolled = enroll(services, consent, visa_card, identity=1)
        client.post(
            f"/v1/customers/{enrolled.customer_id}/palms",
            headers=auth(enrolled),
            files=[
                ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
                for i, f in enumerate(enrollment_samples(20, 3), 1)
            ],
            data={"label": "left"},
        )
        client.delete(f"/v1/customers/{enrolled.customer_id}", headers=auth(enrolled))
        assert services.repository.templates_for(enrolled.customer_id) == []

    def test_suspended_profile_can_still_be_erased(
        self, client, services, consent, visa_card
    ):
        """Regression: erasure once matched only ACTIVE, so a customer who
        paused first and then asked for deletion was refused -- which is the
        most likely order for someone leaving."""
        enrolled = enroll(services, consent, visa_card)
        headers = auth(enrolled)
        client.post(f"/v1/customers/{enrolled.customer_id}/consent/withdraw", headers=headers)

        erased = client.delete(f"/v1/customers/{enrolled.customer_id}", headers=headers)
        assert erased.status_code == 200
        assert erased.json()["erased"]
        assert services.repository.get_profile(enrolled.customer_id).wrapped_dek == b""
