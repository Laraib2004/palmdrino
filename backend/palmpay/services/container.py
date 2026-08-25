"""Composition root.

One place where concrete implementations are chosen, so swapping the software
KMS for an HSM or the mock gateway for real Nexi is a change here and nowhere
else. The API, the CLI and the tests all build their world through this.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..palmprint.registry import BiometricEngine, get_engine
from ..config import Settings, get_settings
from ..crypto.kms import KeyManager, SoftwareKms
from ..payments.gateway import PaymentGateway
from ..payments.nexi_mock import MockNexiGateway
from ..store.repository import Repository
from .enrollment import EnrollmentService
from .payment import PaymentService


@dataclass
class ServiceContainer:
    settings: Settings
    repository: Repository
    kms: KeyManager
    engine: BiometricEngine
    gateway: PaymentGateway
    enrollment: EnrollmentService
    payment: PaymentService

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        gateway: PaymentGateway | None = None,
        kms: KeyManager | None = None,
    ) -> "ServiceContainer":
        settings = settings or get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        repository = Repository(settings.database_path)
        kms = kms or SoftwareKms(settings.keystore_path)
        engine = get_engine(settings.modality, threshold=settings.match_threshold)
        gateway = gateway or MockNexiGateway()

        return cls(
            settings=settings,
            repository=repository,
            kms=kms,
            engine=engine,
            gateway=gateway,
            enrollment=EnrollmentService(
                repository=repository,
                kms=kms,
                engine=engine,
                gateway=gateway,
                settings=settings,
            ),
            payment=PaymentService(
                repository=repository,
                kms=kms,
                engine=engine,
                gateway=gateway,
                settings=settings,
            ),
        )

    def close(self) -> None:
        self.repository.close()
