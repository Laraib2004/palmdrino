"""Runtime configuration.

Every value that changes the security or accuracy posture of the system lives
here rather than being buried in code: the match operating point, whether
liveness is enforced, and where key material lives. All are overridable via
``PALMPAY_*`` environment variables so a deployment never has to edit source.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .palmprint.types import Modality

DEFAULT_DATA_DIR = Path.home() / ".palmdrino"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PALMPAY_", extra="ignore")

    data_dir: Path = DEFAULT_DATA_DIR
    modality: Modality = Modality.PALM_PRINT_RGB

    # -- matching policy ------------------------------------------------------
    # Operating point. Lower = stricter = fewer false accepts, more false
    # rejects.
    #
    # 0.34 comes from scripts/benchmark.py over 80 synthetic identities x 6
    # samples (1200 genuine, 3160 impostor pairs): measured FAR 0.000, FRR
    # 0.0125, against an EER of 0.00057 at 0.390. It sits below the EER point
    # on purpose -- for payments a false accept charges the wrong person, while
    # a false reject just asks them to scan again, so the two errors are not
    # worth trading evenly.
    #
    # This value is valid for the prototype ONLY. Synthetic identities are
    # statistically independent and real palms are not, so real FAR will be
    # worse. Re-run the benchmark against a real palmprint dataset and reset
    # this before the system touches money.
    match_threshold: float = 0.34
    # Minimum distance gap between the best and second-best candidate. If two
    # enrolled palms both look like the presented one, charging either is
    # unacceptable -- so the transaction is refused rather than guessed.
    match_margin: float = 0.04
    # Ceiling on candidates per shard. A shard larger than this means the
    # identifier hint is not narrowing enough and 1:N accuracy is degrading.
    max_candidates: int = 64

    # -- capture policy -------------------------------------------------------
    require_liveness: bool = True
    require_quality: bool = True
    enrollment_samples: int = 3
    # Max distance permitted between two samples of the same palm at
    # enrollment. Catches a user swapping hands mid-enrollment, and a bad ROI.
    # Set just above the genuine p95 of 0.296 from the benchmark run described
    # above, so ordinary capture variation does not block enrollment while a
    # genuinely different hand still does.
    enrollment_consistency_max: float = 0.32

    # -- secrets --------------------------------------------------------------
    # Peppers the identifier-hint HMAC. Never store this next to the database.
    shard_pepper: str = Field(default="")

    @property
    def keystore_path(self) -> Path:
        return self.data_dir / "keystore.json"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "palmdrino.db"

    def resolve_pepper(self) -> bytes:
        """Return the shard pepper, generating and persisting one if unset.

        Auto-generation keeps the prototype runnable out of the box. In
        production this must come from the secret manager via
        ``PALMPAY_SHARD_PEPPER``: a pepper on the same disk as the database it
        protects provides no protection at all if that disk is what leaks.
        """
        if self.shard_pepper:
            return self.shard_pepper.encode("utf-8")

        pepper_file = self.data_dir / "shard_pepper.key"
        if pepper_file.exists():
            return pepper_file.read_bytes()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        pepper = secrets.token_bytes(32)
        pepper_file.write_bytes(pepper)
        return pepper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
