"""Shared fixtures.

Every test gets its own data directory, keystore and database, so nothing
leaks between tests and no test can touch a real deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from palmpay.config import Settings
from palmpay.payments.gateway import CardDetails
from palmpay.payments.nexi_mock import TEST_CARDS, MockNexiGateway
from palmpay.services.container import ServiceContainer
from palmpay.services.enrollment import ConsentGrant


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "palmdrino", shard_pepper="test-pepper-do-not-use")


@pytest.fixture
def gateway() -> MockNexiGateway:
    return MockNexiGateway()


@pytest.fixture
def services(settings: Settings, gateway: MockNexiGateway):
    container = ServiceContainer.build(settings, gateway=gateway)
    yield container
    container.close()


@pytest.fixture
def consent() -> ConsentGrant:
    return ConsentGrant(
        granted=True,
        purposes=("biometric_processing", "payment_execution"),
        policy_version="2026-01-v1",
        evidence_text="I agree to the processing of my palm biometric data.",
    )


@pytest.fixture
def visa_card() -> CardDetails:
    return CardDetails(
        pan=TEST_CARDS["visa_ok"],
        exp_month=12,
        exp_year=2032,
        cvv="123",
        holder_name="Maria Rossi",
    )


@pytest.fixture
def mastercard() -> CardDetails:
    return CardDetails(
        pan=TEST_CARDS["mastercard_ok"],
        exp_month=6,
        exp_year=2031,
        cvv="456",
        holder_name="Luca Bianchi",
    )
