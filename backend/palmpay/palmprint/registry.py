"""Engine registry -- the seam where palm-vein (NIR) will plug in.

Everything above the palmprint package (enrollment, identification, payment,
the HTTP API, the Android client) resolves its engine through ``get_engine``
and never imports a concrete extractor. Adding the NIR modality therefore means
writing three classes and one registry entry; no caller changes.

Templates carry their ``engine_id``, and the matcher refuses to compare across
engines, so a modality migration is an explicit re-enrollment rather than a
silent accuracy regression.
"""

from __future__ import annotations

from dataclasses import dataclass

from .capture import DEFAULT_CAPTURE_CONFIG, CaptureConfig, PalmPrintRegionExtractor
from .features import DEFAULT_COMPCODE_CONFIG, CompetitiveCodeConfig, CompetitiveCodeExtractor
from .matcher import CompetitiveCodeMatcher
from .types import FeatureExtractor, Modality, RegionExtractor


@dataclass(frozen=True)
class BiometricEngine:
    """A complete recognition stack for one modality."""

    modality: Modality
    region_extractor: RegionExtractor
    feature_extractor: FeatureExtractor
    matcher: CompetitiveCodeMatcher

    @property
    def engine_id(self) -> str:
        return (
            f"{self.modality.value}/{self.feature_extractor.algorithm}"
            f"/v{self.feature_extractor.version}"
        )


def _build_palm_print(
    capture_config: CaptureConfig, feature_config: CompetitiveCodeConfig, threshold: float
) -> BiometricEngine:
    return BiometricEngine(
        modality=Modality.PALM_PRINT_RGB,
        region_extractor=PalmPrintRegionExtractor(capture_config),
        feature_extractor=CompetitiveCodeExtractor(feature_config),
        matcher=CompetitiveCodeMatcher(
            threshold=threshold, orientations=feature_config.orientations
        ),
    )


def _build_palm_vein(*_args, **_kwargs) -> BiometricEngine:
    raise NotImplementedError(
        "palm_vein_nir is not implemented yet. To add it, provide: "
        "(1) a RegionExtractor that segments the palm from an NIR frame, "
        "(2) a FeatureExtractor producing Template(modality=PALM_VEIN_NIR, ...), "
        "(3) a matcher and a threshold derived from a fresh FAR/FRR sweep -- "
        "the palm-print threshold does not transfer. Then register the builder "
        "here. No code outside this package needs to change."
    )


_BUILDERS = {
    Modality.PALM_PRINT_RGB: _build_palm_print,
    Modality.PALM_VEIN_NIR: _build_palm_vein,
}


def get_engine(
    modality: Modality = Modality.PALM_PRINT_RGB,
    *,
    capture_config: CaptureConfig = DEFAULT_CAPTURE_CONFIG,
    feature_config: CompetitiveCodeConfig = DEFAULT_COMPCODE_CONFIG,
    threshold: float = 0.32,
) -> BiometricEngine:
    """Resolve the recognition stack for a modality."""
    try:
        builder = _BUILDERS[modality]
    except KeyError:
        raise ValueError(f"unknown modality: {modality}") from None
    return builder(capture_config, feature_config, threshold)


def available_modalities() -> list[Modality]:
    """Modalities that are actually usable right now."""
    usable: list[Modality] = []
    for modality in _BUILDERS:
        try:
            get_engine(modality)
        except NotImplementedError:
            continue
        usable.append(modality)
    return usable
