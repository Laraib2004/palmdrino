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


def auth(enrolled: dict) -> dict:
    """Authorization header for the customer created by ``enroll_via_api``."""
    return {"Authorization": f"Bearer {enrolled['credential']}"}


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
        enrolled = enroll_via_api(client).json()
        customer_id = enrolled["customer_id"]
        headers = auth(enrolled)

        fetched = client.get(f"/v1/customers/{customer_id}", headers=headers).json()
        assert fetched["status"] == "active"
        assert fetched["card_display"] == "Visa ****1111"
        assert fetched["consent_active"]

        erased = client.delete(f"/v1/customers/{customer_id}", headers=headers).json()
        assert erased["erased"]

    def test_erasure_revokes_the_credential(self, client):
        """A device holding a credential must not keep authenticating."""
        enrolled = enroll_via_api(client).json()
        customer_id = enrolled["customer_id"]
        headers = auth(enrolled)

        client.delete(f"/v1/customers/{customer_id}", headers=headers)
        after = client.get(f"/v1/customers/{customer_id}", headers=headers)
        assert after.status_code == 401

    def test_erased_palm_can_no_longer_pay(self, client):
        enrolled = enroll_via_api(client).json()
        client.delete(f"/v1/customers/{enrolled['customer_id']}", headers=auth(enrolled))
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=9)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert response.status_code == 402


class TestAudit:
    def test_lists_events(self, client):
        enroll_via_api(client)
        events = client.get("/v1/audit").json()
        assert any(e["event_type"] == "enrollment" for e in events)


class TestCustomerAuthorization:
    """PD-29: a customer credential grants access to that customer, and no other.

    Before this existed, knowing a customer id was enough to read someone's
    linked card and erase their profile -- which only held up while the sole
    callers were a few trusted terminals. Under a customer-facing app it does
    not, so these are the tests that must not regress.
    """

    def test_enrollment_returns_a_credential_once(self, client):
        body = enroll_via_api(client).json()
        assert body["credential"].startswith(body["customer_id"] + ".")

    def test_credential_is_not_stored_in_recoverable_form(self, client, services):
        """A database leak must not yield anything replayable."""
        body = enroll_via_api(client).json()
        secret = body["credential"].split(".", 1)[1]
        stored = services.repository.active_credentials(body["customer_id"])
        assert len(stored) == 1
        assert secret not in stored[0].token_hash

    def test_unauthenticated_access_is_refused(self, client):
        customer_id = enroll_via_api(client).json()["customer_id"]
        assert client.get(f"/v1/customers/{customer_id}").status_code == 401
        assert client.delete(f"/v1/customers/{customer_id}").status_code == 401

    def test_malformed_credential_is_refused(self, client):
        customer_id = enroll_via_api(client).json()["customer_id"]
        for header in ("Bearer nonsense", "Bearer ", "notbearer x", f"Bearer {customer_id}"):
            response = client.get(
                f"/v1/customers/{customer_id}", headers={"Authorization": header}
            )
            assert response.status_code == 401, header

    def test_one_customer_cannot_read_another(self, client):
        victim = enroll_via_api(client, identity=1).json()
        attacker = enroll_via_api(client, identity=2, pan=TEST_CARDS["mastercard_ok"]).json()

        response = client.get(
            f"/v1/customers/{victim['customer_id']}", headers=auth(attacker)
        )
        assert response.status_code == 403

    def test_one_customer_cannot_erase_another(self, client, services):
        """The worst version of the old hole: erasure is irreversible."""
        victim = enroll_via_api(client, identity=1).json()
        attacker = enroll_via_api(client, identity=2, pan=TEST_CARDS["mastercard_ok"]).json()

        response = client.delete(
            f"/v1/customers/{victim['customer_id']}", headers=auth(attacker)
        )
        assert response.status_code == 403
        assert services.repository.get_profile(victim["customer_id"]).is_active

    def test_forbidden_response_does_not_leak_existence(self, client):
        """403 for both real and invented ids, so ids cannot be probed."""
        enrolled = enroll_via_api(client).json()
        other = enroll_via_api(client, identity=2, pan=TEST_CARDS["mastercard_ok"]).json()

        real = client.get(f"/v1/customers/{other['customer_id']}", headers=auth(enrolled))
        invented = client.get("/v1/customers/cus_does_not_exist", headers=auth(enrolled))
        assert real.status_code == invented.status_code == 403
        assert real.json()["code"] == invented.json()["code"]

    def test_terminal_key_cannot_reach_customer_accounts(self, client, monkeypatch):
        """Terminal and customer grants are separate, not a hierarchy."""
        monkeypatch.setenv("PALMPAY_API_KEY", "terminal-secret")
        customer_id = enroll_via_api(client).json()["customer_id"]
        response = client.get(
            f"/v1/customers/{customer_id}", headers={"X-Api-Key": "terminal-secret"}
        )
        assert response.status_code == 401

    def test_customer_credential_cannot_take_payments(self, client, monkeypatch):
        """And the reverse: a customer cannot charge cards."""
        enrolled = enroll_via_api(client).json()
        monkeypatch.setenv("PALMPAY_API_KEY", "terminal-secret")
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
            headers=auth(enrolled),
        )
        assert response.status_code == 401

    def test_audit_requires_the_admin_grant(self, client, monkeypatch):
        """The audit log records who paid what, where. Not for terminals."""
        monkeypatch.setenv("PALMPAY_ADMIN_KEY", "admin-secret")
        monkeypatch.setenv("PALMPAY_API_KEY", "terminal-secret")
        assert client.get("/v1/audit", headers={"X-Api-Key": "terminal-secret"}).status_code == 401
        assert client.get("/v1/audit", headers={"X-Admin-Key": "admin-secret"}).status_code == 200

    def test_self_enrollment_needs_no_prior_credential(self, client, monkeypatch):
        """A customer installing the app has nothing yet -- enrollment must be open."""
        monkeypatch.setenv("PALMPAY_API_KEY", "terminal-secret")
        assert enroll_via_api(client).status_code == 200


class TestRateLimiting:
    """PD-07: enrollment and capture-check are open, so they must be bounded."""

    def test_pay_attempts_against_one_code_are_capped(self, client):
        """An attacker who guesses at a pay code must not get unlimited tries."""
        enroll_via_api(client)
        last = None
        for _ in range(12):
            last = client.post(
                "/v1/pay",
                files={"image": ("p.jpg", jpeg(sample(42, seed=1)), "image/jpeg")},
                data={"hint": HINT, "amount_minor": 100, "merchant_id": "mrc_1"},
            )
        assert last.status_code == 429
        assert last.json()["code"] == "rate_limited"
        assert int(last.headers["Retry-After"]) > 0

    def test_a_different_code_is_not_blocked(self, client):
        """Limits are per identity, not global -- one attacker must not
        lock every other customer out of paying."""
        enroll_via_api(client)
        for _ in range(12):
            client.post(
                "/v1/pay",
                files={"image": ("p.jpg", jpeg(sample(42, seed=1)), "image/jpeg")},
                data={"hint": "1111", "amount_minor": 100, "merchant_id": "mrc_1"},
            )
        response = client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 1000, "merchant_id": "mrc_1"},
        )
        assert response.status_code == 200

    def test_enrollment_is_capped(self, client):
        for index in range(5):
            enroll_via_api(client, identity=index + 1)
        assert enroll_via_api(client, identity=7).status_code == 429

    def test_rate_limit_bucket_does_not_store_the_pay_code(self, client, services):
        """The limiter table must not become a directory of live pay codes."""
        enroll_via_api(client)
        client.post(
            "/v1/pay",
            files={"image": ("p.jpg", jpeg(sample(1, seed=44)), "image/jpeg")},
            data={"hint": HINT, "amount_minor": 100, "merchant_id": "mrc_1"},
        )
        rows = services.repository._conn.execute("SELECT bucket FROM rate_limits").fetchall()
        assert rows
        assert all(HINT not in row["bucket"] for row in rows)

    def test_blocks_are_audited(self, client, services):
        enroll_via_api(client)
        for _ in range(12):
            client.post(
                "/v1/pay",
                files={"image": ("p.jpg", jpeg(sample(42, seed=1)), "image/jpeg")},
                data={"hint": HINT, "amount_minor": 100, "merchant_id": "mrc_1"},
            )
        events = services.repository.recent_audit(limit=100)
        assert any(e.event_type == "rate_limit" and e.outcome == "blocked" for e in events)
