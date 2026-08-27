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
from .account import AccountService
from .credentials import CredentialService
from .enrollment import EnrollmentService
from .payment import DurableLowValueTracker, PaymentService
from .maintenance import KeyMaintenance
from .ratelimit import RateLimiter


@dataclass
class ServiceContainer:
    settings: Settings
    repository: Repository
    kms: KeyManager
    engine: BiometricEngine
    gateway: PaymentGateway
    credentials: CredentialService
    rate_limiter: RateLimiter
    enrollment: EnrollmentService
    payment: PaymentService
    account: AccountService

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
        credentials = CredentialService(
            repository=repository, pepper=settings.resolve_pepper()
        )
        rate_limiter = RateLimiter(repository=repository)

        return cls(
            settings=settings,
            repository=repository,
            kms=kms,
            engine=engine,
            gateway=gateway,
            credentials=credentials,
            rate_limiter=rate_limiter,
            enrollment=EnrollmentService(
                repository=repository,
                kms=kms,
                engine=engine,
                gateway=gateway,
                settings=settings,
                credentials=credentials,
            ),
            account=AccountService(
                repository=repository, kms=kms, gateway=gateway
            ),
            payment=PaymentService(
                low_value_tracker=DurableLowValueTracker(repository=repository),
                repository=repository,
                kms=kms,
                engine=engine,
                gateway=gateway,
                settings=settings,
            ),
        )

    def key_maintenance(self) -> KeyMaintenance:
        """Build the KEK re-wrap job (PD-14).

        Not a stored field: it is scheduled work, not a request-path
        dependency, and constructing it on demand keeps that distinction clear.
        """
        return KeyMaintenance(repository=self.repository, kms=self.kms)

    def close(self) -> None:
        self.repository.close()
