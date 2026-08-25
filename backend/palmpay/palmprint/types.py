"""Core biometric types.

The whole recognition core is written against the interfaces in this module so
that the current palm-print (RGB) engine can be swapped for a palm-vein (NIR)
engine later without touching capture orchestration, storage, matching policy
or the payment layer. See ``registry.py`` for how engines are selected.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

import numpy as np

_MAGIC = b"PDT1"  # Palmdrino Template, container format v1


class Modality(str, Enum):
    """Sensing modality a template was produced from.

    Templates of different modalities are never comparable; the matcher
    refuses cross-modality distance computation outright.
    """

    PALM_PRINT_RGB = "palm_print_rgb"
    PALM_VEIN_NIR = "palm_vein_nir"


@dataclass(frozen=True)
class QualityReport:
    """Outcome of the capture quality gate.

    ``ok`` False means the frame must not be used for enrollment or matching.
    ``reasons`` carries machine-readable codes the client can turn into user
    guidance ("hold still", "move closer", "more light").
    """

    ok: bool
    sharpness: float
    exposure: float
    contrast: float
    coverage: float
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "sharpness": round(self.sharpness, 4),
            "exposure": round(self.exposure, 4),
            "contrast": round(self.contrast, 4),
            "coverage": round(self.coverage, 4),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PalmROI:
    """A normalised, square, grayscale palm region ready for feature extraction."""

    image: np.ndarray
    modality: Modality
    quality: QualityReport

    def __post_init__(self) -> None:
        if self.image.ndim != 2:
            raise ValueError("ROI image must be single-channel grayscale")
        if self.image.shape[0] != self.image.shape[1]:
            raise ValueError("ROI image must be square")


@dataclass(frozen=True)
class Template:
    """An irreversible feature representation of a palm.

    Still biometric data under GDPR Art. 9 even though the original image
    cannot be reconstructed from it -- it is always stored encrypted under the
    owning customer's DEK, never in the clear.

    ``codes`` holds the per-pixel feature code (for competitive code: the index
    of the winning Gabor orientation). ``mask`` marks pixels that carry usable
    signal; low-energy pixels are excluded from the distance computation.
    """

    modality: Modality
    algorithm: str
    version: int
    codes: np.ndarray
    mask: np.ndarray

    def __post_init__(self) -> None:
        if self.codes.shape != self.mask.shape:
            raise ValueError("codes and mask must have identical shape")
        if self.codes.dtype != np.uint8 or self.mask.dtype != np.uint8:
            raise ValueError("codes and mask must both be uint8")

    @property
    def engine_id(self) -> str:
        """Identifies the exact engine build a template came from.

        Templates are only comparable when this matches -- an engine upgrade
        means re-enrollment, not silent cross-version matching.
        """
        return f"{self.modality.value}/{self.algorithm}/v{self.version}"

    def serialize(self) -> bytes:
        """Pack to bytes for encryption at rest.

        Deliberately avoids pickle: templates round-trip through storage and a
        deserializer that can execute code is not acceptable in this position,
        even behind encryption.
        """
        header = json.dumps(
            {
                "modality": self.modality.value,
                "algorithm": self.algorithm,
                "version": self.version,
                "shape": list(self.codes.shape),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return b"".join(
            [
                _MAGIC,
                struct.pack("<I", len(header)),
                header,
                self.codes.tobytes(order="C"),
                self.mask.tobytes(order="C"),
            ]
        )

    @classmethod
    def deserialize(cls, blob: bytes) -> "Template":
        if blob[:4] != _MAGIC:
            raise ValueError("not a Palmdrino template container")
        (header_len,) = struct.unpack("<I", blob[4:8])
        header = json.loads(blob[8 : 8 + header_len].decode("utf-8"))
        shape = tuple(header["shape"])
        n = int(np.prod(shape))
        body = blob[8 + header_len :]
        if len(body) != 2 * n:
            raise ValueError("template payload truncated")
        codes = np.frombuffer(body[:n], dtype=np.uint8).reshape(shape)
        mask = np.frombuffer(body[n:], dtype=np.uint8).reshape(shape)
        return cls(
            modality=Modality(header["modality"]),
            algorithm=header["algorithm"],
            version=int(header["version"]),
            codes=codes.copy(),
            mask=mask.copy(),
        )


@runtime_checkable
class FeatureExtractor(Protocol):
    """Contract every recognition engine must satisfy.

    A future NIR/vein engine implements exactly this and registers itself in
    ``registry.py``; nothing downstream changes.
    """

    modality: Modality
    algorithm: str
    version: int

    def extract(self, roi: PalmROI) -> Template: ...


@runtime_checkable
class RegionExtractor(Protocol):
    """Turns a raw captured frame into a normalised palm ROI.

    Separate from ``FeatureExtractor`` because ROI localisation is
    sensor-dependent (skin segmentation for RGB, vein contrast for NIR) while
    feature extraction is algorithm-dependent.
    """

    modality: Modality

    def locate(self, frame: np.ndarray) -> PalmROI | None: ...
