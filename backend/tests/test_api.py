"""HTTP API contract -- this is what the Android client codes against."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from palmpay.api.main import app, set_container
from palmpay.payments.nexi_mock import TEST_CARDS

from .synthetic import enrollment_samples, flat_print, sample

HINT = "4821"


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


def enroll_via_api(client, identity=1, hint=HINT, hint_type="secret", pan=None):
    files = [
        ("frames", (f"f{index}.jpg", jpeg(frame), "image/jpeg"))
        for index, frame in enumerate(enrollment_samples(identity, 3), 1)
    ]
    return client.post(
        "/v1/enroll",
        files=files,
        data={
            "hint": hint,
            "hint_type": hint_type,
            "card_number": pan or TEST_CARDS["visa_ok"],
            "card_exp_month": 12,
            "card_exp_year": 2032,
            "card_cvv": "123",
            "card_holder": "Maria Rossi",
            "pii": '{"name": "Maria Rossi", "email": "maria@example.it"}',
            "consent_granted": "true",
            "consent_purposes": "biometric_processing,payment_execution",
            "consent_policy_version": "2026-01-v1",
            "consent_evidence": "I agree to palm biometric processing.",
        },
    )


class TestHealth:
    def test_reports_configuration(self, client):
        body = client.get("/v1/health").json()
        assert body["status"] == "ok"
        assert body["engine_id"] == "palm_print_rgb/competitive_code/v1"
        assert body["gateway"] == "nexi-mock"
        assert body["enrollment_samples"] == 3


class TestCaptureCheck:
    def test_accepts_a_good_frame(self, client):
        response = client.post(
            "/v1/capture/check", files={"image": ("p.jpg", jpeg(sample(1, seed=1)), "image/jpeg")}
        )
        body = response.json()
        assert response.status_code == 200
        assert body["palm_found"] and body["usable"]
        assert body["guidance"] == []

    def test_returns_guidance_for_a_spoof(self, client):
        body = client.post(
            "/v1/capture/check", files={"image": ("p.jpg", jpeg(flat_print(1)), "image/jpeg")}
        ).json()
        assert not body["usable"]
        assert body["guidance"], "client needs something to tell the user"

    def test_rejects_undecodable_upload(self, client):
        response = client.post(
            "/v1/capture/check", files={"image": ("x.jpg", b"not an image", "image/jpeg")}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "undecodable_image"


class TestEnrollment:
    def test_enrolls_successfully(self, client):
        response = enroll_via_api(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["customer_id"].startswith("cus_")
        assert body["card_display"] == "Visa ****1111"
        assert body["hint_type"] == "secret"
        assert len(body["quality"]) == 3

    def test_mastercard_enrolls_the_same_way(self, client):
        body = enroll_via_api(client, identity=2, pan=TEST_CARDS["mastercard_ok"]).json()
        assert body["card_display"] == "Mastercard ****4444"

    def test_rejects_missing_consent(self, client):
        files = [
            ("frames", (f"f{i}.jpg", jpeg(f), "image/jpeg"))
            for i, f in enumerate(enrollment_samples(1, 3), 1)
        ]
        response = client.post(
            "/v1/enroll",
            files=files,
            data={
                "hint": HINT,
                "card_number": TEST_CARDS["visa_ok"],
                "card_exp_month": 12,
                "card_exp_year": 2032,
                "card_cvv": "123",
                "consent_granted": "false",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "consent_required"

    def test_rejects_invalid_card(self, client):
        response = enroll_via_api(client, pan="4111111111111112")
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_card"

    def test_rejects_unsupported_scheme(self, client):
        response = enroll_via_api(client, pan="6011111111111117")
        assert response.status_code == 400


class TestPayment:
    def test_charges_the_linked_card(self, client):
        enrolled = enroll_via_api(client).json()
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, rotation_deg=2.0, seed=44)), "image/jpeg")},
            data={
                "hint": HINT,
                "amount_minor": 24_990,
                "currency": "EUR",
                "merchant_id": "mrc_bar_roma",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["customer_id"] == enrolled["customer_id"]
        assert body["amount_minor"] == 24_990
        assert body["card_display"] == "Visa ****1111"
        assert body["sca"]["strongly_authenticated"]

    def test_declines_an_unenrolled_palm(self, client):
        enroll_via_api(client)
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(42, seed=2)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert response.status_code == 402
        assert response.json()["code"] == "no_match"

    def test_declines_a_spoof(self, client):
        enroll_via_api(client)
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(flat_print(1)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert response.status_code == 402
        assert response.json()["code"] == "liveness_failed"

    def test_response_never_leaks_biometric_or_card_data(self, client):
        enroll_via_api(client)
        raw = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        ).text
        assert TEST_CARDS["visa_ok"] not in raw
        assert "codes" not in raw and "template" not in raw


class TestCustomerLifecycle:
    def test_fetch_and_erase(self, client):
        customer_id = enroll_via_api(client).json()["customer_id"]

        fetched = client.get(f"/v1/customers/{customer_id}").json()
        assert fetched["status"] == "active"
        assert fetched["card_display"] == "Visa ****1111"
        assert fetched["consent_active"]

        erased = client.delete(f"/v1/customers/{customer_id}").json()
        assert erased["erased"]

        after = client.get(f"/v1/customers/{customer_id}").json()
        assert after["status"] == "shredded"
        assert after["card_display"] is None
        assert not after["consent_active"]

    def test_erased_palm_can_no_longer_pay(self, client):
        customer_id = enroll_via_api(client).json()["customer_id"]
        client.delete(f"/v1/customers/{customer_id}")
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=9)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert response.status_code == 402

    def test_unknown_customer_is_404(self, client):
        assert client.get("/v1/customers/cus_nope").status_code == 404


class TestAudit:
    def test_lists_events(self, client):
        enroll_via_api(client)
        events = client.get("/v1/audit").json()
        assert any(e["event_type"] == "enrollment" for e in events)
