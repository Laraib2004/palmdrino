"""Feature extraction: competitive code for palm-print.

Competitive code convolves the ROI with a bank of Gabor filters at evenly
spaced orientations and records, per pixel, *which* orientation responded most
strongly. The resulting map encodes the dominant line direction at every point
-- which is exactly what a palm print is: a pattern of creases and ridges.

Why this algorithm for the prototype:

* No training data and no model weights. It is deterministic and auditable,
  which matters when the output authorises payments and has to be explainable
  to a regulator.
* It is the established baseline in the palmprint literature, so published
  FAR/FRR numbers are directly comparable to what ``benchmark.py`` produces.
* The template is compact and the original image cannot be reconstructed from
  it.

It is a baseline, not the endgame. A learned embedding will beat it on accuracy
at scale; the ``FeatureExtractor`` interface exists so that swap costs nothing
downstream.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .types import Modality, PalmROI, Template


@dataclass(frozen=True)
class CompetitiveCodeConfig:
    """Gabor bank geometry and template resolution.

    ``orientations`` sets the angular quantisation: 6 is the value used in the
    original competitive code work and balances discriminative power against
    sensitivity to small rotations left over after ROI normalisation.

    ``stride`` downsamples the code map. Neighbouring pixels in a Gabor
    response are highly correlated, so a stride of 2 discards almost no
    information while cutting template size and match cost by 4x.
    """

    orientations: int = 6
    kernel_size: int = 35
    sigma: float = 4.0
    wavelength: float = 8.0
    gamma: float = 0.5
    stride: int = 2
    mask_percentile: float = 15.0


DEFAULT_COMPCODE_CONFIG = CompetitiveCodeConfig()


def build_gabor_bank(cfg: CompetitiveCodeConfig) -> list[np.ndarray]:
    """Build evenly spaced Gabor kernels spanning 0..pi.

    Orientations span pi rather than 2*pi because a line has no direction: an
    edge at theta and at theta+pi are the same crease.
    """
    kernels: list[np.ndarray] = []
    for index in range(cfg.orientations):
        theta = np.pi * index / cfg.orientations
        kernel = cv2.getGaborKernel(
            ksize=(cfg.kernel_size, cfg.kernel_size),
            sigma=cfg.sigma,
            theta=theta,
            lambd=cfg.wavelength,
            gamma=cfg.gamma,
            psi=0.0,
            ktype=cv2.CV_32F,
        )
        # Zero-mean the kernel so uniform illumination produces no response;
        # this is what makes the code robust to brightness shifts.
        kernel -= kernel.mean()
        kernels.append(kernel)
    return kernels


class CompetitiveCodeExtractor:
    """Palm-print feature extractor. Implements ``FeatureExtractor``."""

    modality = Modality.PALM_PRINT_RGB
    algorithm = "competitive_code"
    version = 1

    def __init__(self, config: CompetitiveCodeConfig = DEFAULT_COMPCODE_CONFIG) -> None:
        if config.orientations < 2 or config.orientations > 255:
            raise ValueError("orientations must be between 2 and 255")
        self.config = config
        self._bank = build_gabor_bank(config)

    def extract(self, roi: PalmROI) -> Template:
        if roi.modality is not self.modality:
            raise ValueError(
                f"extractor handles {self.modality.value}, got {roi.modality.value}"
            )

        cfg = self.config
        image = roi.image.astype(np.float32) / 255.0

        responses = np.stack(
            [cv2.filter2D(image, cv2.CV_32F, kernel) for kernel in self._bank],
            axis=0,
        )

        # Palm creases are dark valleys, so the *most negative* response marks
        # the winning orientation.
        codes = np.argmin(responses, axis=0).astype(np.uint8)

        # Confidence: how decisively the winner beat the field. Where all
        # orientations respond alike the pixel carries no orientation
        # information and must not be allowed to vote during matching.
        energy = responses.max(axis=0) - responses.min(axis=0)

        codes = codes[:: cfg.stride, :: cfg.stride]
        energy = energy[:: cfg.stride, :: cfg.stride]

        cutoff = float(np.percentile(energy, cfg.mask_percentile))
        mask = (energy > cutoff).astype(np.uint8)

        return Template(
            modality=self.modality,
            algorithm=self.algorithm,
            version=self.version,
            codes=np.ascontiguousarray(codes),
            mask=np.ascontiguousarray(mask),
        )
