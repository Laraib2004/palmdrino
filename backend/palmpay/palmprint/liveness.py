"""Presentation-attack detection (liveness).

WHAT THIS IS: a set of passive image-statistics heuristics that reject the
cheap attacks -- a photo on a phone screen, a printed palm, a flat replica.
They exploit the fact that a re-captured image loses fine texture, gains
screen-door or halftone periodicity, and shows specular behaviour a real hand
does not.

WHAT THIS IS NOT: certified anti-spoofing. Under this design the palm is the
SCA inherence factor, so a defeated liveness check means unauthenticated
payments. Before any real money moves this must be replaced or backed by:

* an active challenge (ask the hand to move, and verify parallax between
  frames), and
* multi-spectral or NIR capture, which is what makes the vein modality hard to
  spoof in the first place, and
* an independent PAD evaluation against ISO/IEC 30107-3.

The scoring is deliberately conservative and every component is reported
separately so that a rejection can be explained rather than being an opaque
verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .capture import to_grayscale


@dataclass(frozen=True)
class LivenessConfig:
    min_high_freq_ratio: float = 0.055
    max_specular_fraction: float = 0.06
    # Robust z-score above local spectral background. Calibrated in
    # tests/test_liveness.py against live, print and simulated-screen captures.
    max_moire_peak: float = 8.0
    min_chroma_std: float = 2.0
    high_freq_cutoff: float = 0.35
    moire_band_low: float = 0.12
    moire_band_high: float = 0.85
    specular_level: int = 250


DEFAULT_LIVENESS_CONFIG = LivenessConfig()


@dataclass(frozen=True)
class LivenessReport:
    passed: bool
    high_freq_ratio: float
    specular_fraction: float
    moire_peak: float
    chroma_std: float
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "high_freq_ratio": round(self.high_freq_ratio, 5),
            "specular_fraction": round(self.specular_fraction, 5),
            "moire_peak": round(self.moire_peak, 4),
            "chroma_std": round(self.chroma_std, 4),
            "reasons": list(self.reasons),
        }


def _radial_frequency_grid(shape: tuple[int, int]) -> np.ndarray:
    """Normalised distance from the DC term for every spectrum bin."""
    height, width = shape
    fy = np.fft.fftshift(np.fft.fftfreq(height))[:, None]
    fx = np.fft.fftshift(np.fft.fftfreq(width))[None, :]
    radius = np.sqrt(fy**2 + fx**2)
    return radius / radius.max()


def _spectrum_stats(gray: np.ndarray, cfg: LivenessConfig) -> tuple[float, float]:
    """Return (high-frequency energy ratio, moire peak prominence).

    A live palm has broadband texture from its creases. A print or a screen
    re-capture is low-pass filtered by the reproduction process, so its energy
    collapses toward the low frequencies -- while a screen additionally adds a
    sharp periodic moire peak that a natural surface never produces.

    The moire measure is a robust local prominence, not a max-to-average ratio.
    A plain ratio mostly measures how low-pass an image is: any blurred image
    scores high because its band average collapses, which flags live palms and
    misses the thing being looked for. Prominence instead asks the right
    question -- does one bin stand out sharply from its immediate neighbours --
    which is what periodic interference actually looks like and what natural
    texture never does.
    """
    image = gray.astype(np.float32) / 255.0
    # Window the image so the frame edges do not leak into the spectrum as a
    # false broadband component.
    window = np.outer(np.hanning(image.shape[0]), np.hanning(image.shape[1]))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(image * window)))

    radius = _radial_frequency_grid(image.shape)
    non_dc = radius > 0.02
    total = float(spectrum[non_dc].sum())
    if total <= 0:
        return 0.0, 0.0

    high_band = non_dc & (radius >= cfg.high_freq_cutoff)
    high_freq_ratio = float(spectrum[high_band].sum() / total)
    moire_peak = _moire_prominence(spectrum, radius, cfg)
    return high_freq_ratio, moire_peak


def _moire_prominence(
    spectrum: np.ndarray, radius: np.ndarray, cfg: LivenessConfig
) -> float:
    """How far the sharpest spectral spike rises above its local background.

    Reported as a robust z-score (median/MAD), so the value does not depend on
    overall image energy.
    """
    log_spectrum = np.log1p(spectrum).astype(np.float32)
    # Median filtering estimates the smooth background the spike sits on.
    background = cv2.medianBlur(log_spectrum, 5)
    residual = log_spectrum - background

    height, width = spectrum.shape
    fy = np.abs(np.fft.fftshift(np.fft.fftfreq(height)))[:, None]
    fx = np.abs(np.fft.fftshift(np.fft.fftfreq(width)))[None, :]
    # Exclude the frequency axes: image borders and any residual DC leakage
    # concentrate there and would masquerade as periodic structure.
    off_axis = (fy > 0.03) & (fx > 0.03)

    band = off_axis & (radius >= cfg.moire_band_low) & (radius <= cfg.moire_band_high)
    if band.sum() < 32:
        return 0.0

    values = residual[band]
    centre = float(np.median(values))
    mad = float(np.median(np.abs(values - centre)))
    scale = 1.4826 * mad  # MAD -> standard-deviation equivalent for normal data
    if scale < 1e-6:
        return 0.0
    return float((values.max() - centre) / scale)


def _chroma_std(frame: np.ndarray) -> float:
    """Spread of skin chrominance.

    Real skin varies in chroma across the palm (blood perfusion, shadowing).
    Print and screen reproductions compress that variation. Grayscale input
    carries no chroma at all, so it is reported as 0 and the corresponding
    check is skipped rather than failed.
    """
    if frame.ndim != 3 or frame.shape[2] < 3:
        return 0.0
    ycrcb = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2YCrCb)
    return float((ycrcb[:, :, 1].std() + ycrcb[:, :, 2].std()) / 2.0)


def assess_liveness(
    frame: np.ndarray, config: LivenessConfig = DEFAULT_LIVENESS_CONFIG
) -> LivenessReport:
    """Run the passive presentation-attack checks on a captured frame."""
    gray = to_grayscale(frame)
    high_freq_ratio, moire_peak = _spectrum_stats(gray, config)
    specular_fraction = float((gray >= config.specular_level).mean())
    chroma_std = _chroma_std(frame)
    has_colour = frame.ndim == 3 and frame.shape[2] >= 3

    reasons: list[str] = []
    if high_freq_ratio < config.min_high_freq_ratio:
        reasons.append("texture_too_flat")
    if specular_fraction > config.max_specular_fraction:
        reasons.append("specular_glare")
    if moire_peak > config.max_moire_peak:
        reasons.append("screen_moire")
    if has_colour and chroma_std < config.min_chroma_std:
        reasons.append("chroma_too_uniform")

    return LivenessReport(
        passed=not reasons,
        high_freq_ratio=high_freq_ratio,
        specular_fraction=specular_fraction,
        moire_peak=moire_peak,
        chroma_std=chroma_std,
        reasons=tuple(reasons),
    )
