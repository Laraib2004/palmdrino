"""Deterministic synthetic palms for testing.

These are NOT a substitute for benchmarking on real palmprints. They exist so
the pipeline, the crypto and the payment flow can be tested end to end without
collecting biometric data from real people -- which, for a system in this
regulatory position, is exactly the right order to do things in: prove the
plumbing on synthetic data, and only then run a properly consented data
collection.

Each ``identity`` seed produces a stable crease pattern. ``sample`` applies the
kinds of variation a real re-capture has -- translation, small rotation,
brightness change, sensor noise -- so genuine and impostor comparisons are
both meaningful.
"""

from __future__ import annotations

import cv2
import numpy as np

SKIN_BGR = (118, 150, 198)
BACKGROUND_BGR = (64, 52, 46)

# Finger geometry as fractions of image size: (base_x, base_y, length, width,
# tilt in degrees from vertical). Spread fingers, because the gaps between them
# are what the ROI localiser keys on.
_FINGERS = [
    (0.355, 0.470, 0.250, 0.068, -15.0),
    (0.440, 0.440, 0.290, 0.072, -5.0),
    (0.530, 0.445, 0.275, 0.070, 5.0),
    (0.615, 0.480, 0.220, 0.062, 16.0),
]
_THUMB = (0.330, 0.640, 0.215, 0.082, -68.0)


def _capsule(canvas: np.ndarray, size: int, spec: tuple, value: int) -> None:
    """Draw one finger as a thick line, which renders as a rounded capsule."""
    base_x, base_y, length, width, tilt = spec
    start = (int(base_x * size), int(base_y * size))
    angle = np.deg2rad(tilt - 90.0)  # -90 deg points up the image
    end = (
        int(start[0] + length * size * np.cos(angle)),
        int(start[1] + length * size * np.sin(angle)),
    )
    cv2.line(canvas, start, end, value, thickness=int(width * size), lineType=cv2.LINE_AA)


def hand_mask(size: int = 256) -> np.ndarray:
    """Silhouette of a spread hand: palm plus four fingers and a thumb.

    The shape matters as much as the texture. Without finger gaps the ROI
    localiser cannot find convexity defects, silently falls back to a centre
    crop, and the rotation-normalising code path -- the part that has to work
    for a handheld capture -- never gets tested.
    """
    mask = np.zeros((size, size), dtype=np.uint8)
    cv2.ellipse(
        mask,
        center=(int(0.49 * size), int(0.655 * size)),
        axes=(int(0.185 * size), int(0.215 * size)),
        angle=0,
        startAngle=0,
        endAngle=360,
        color=255,
        thickness=-1,
    )
    for finger in _FINGERS:
        _capsule(mask, size, finger, 255)
    _capsule(mask, size, _THUMB, 255)
    return mask


def _curve(rng: np.random.Generator, size: int) -> np.ndarray:
    """A smooth polyline standing in for a palm crease."""
    start = np.array(
        [rng.uniform(0.30, 0.70) * size, rng.uniform(0.48, 0.85) * size], dtype=np.float64
    )
    heading = rng.uniform(0, 2 * np.pi)
    points = [start]
    step = size / 22.0
    for _ in range(rng.integers(5, 10)):
        heading += rng.normal(0, 0.40)
        nxt = points[-1] + step * np.array([np.cos(heading), np.sin(heading)])
        points.append(np.clip(nxt, 0, size - 1))
    return np.array(points, dtype=np.int32)


def palm_image(identity: int, size: int = 256, *, hand_shape: bool = True) -> np.ndarray:
    """Base BGR image for one synthetic identity.

    ``hand_shape=False`` yields a full-frame palm surface with no silhouette,
    which stands in for an already-cropped dataset image.
    """
    rng = np.random.default_rng(identity * 7919 + 13)
    image = np.zeros((size, size, 3), dtype=np.float32)
    for channel, value in enumerate(SKIN_BGR):
        image[:, :, channel] = value

    # Uneven illumination, as a real palm under ambient light has.
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) / size
    shading = 1.0 + 0.12 * np.sin(2.4 * yy + 0.6) - 0.10 * np.cos(1.8 * xx)
    image *= shading[:, :, None]

    creases = np.zeros((size, size), dtype=np.float32)
    for _ in range(rng.integers(30, 40)):
        cv2.polylines(
            creases,
            [_curve(rng, size)],
            isClosed=False,
            color=float(rng.uniform(0.35, 0.85)),
            thickness=int(rng.integers(1, 3)),
            lineType=cv2.LINE_AA,
        )
    creases = cv2.GaussianBlur(creases, (3, 3), 0)
    image *= (1.0 - 0.42 * creases)[:, :, None]

    # Fine texture and per-channel variation: without these the image reads as
    # a flat print to the liveness check, which is the correct behaviour.
    image += rng.normal(0, 4.5, (size, size, 3))
    image = np.clip(image, 0, 255)

    if not hand_shape:
        return image.astype(np.uint8)

    background = np.zeros_like(image)
    for channel, value in enumerate(BACKGROUND_BGR):
        background[:, :, channel] = value
    background += np.random.default_rng(identity + 5).normal(0, 3.0, image.shape)

    mask = hand_mask(size).astype(np.float32) / 255.0
    mask = cv2.GaussianBlur(mask, (5, 5), 1.0)[:, :, None]
    blended = image * mask + np.clip(background, 0, 255) * (1.0 - mask)
    return np.clip(blended, 0, 255).astype(np.uint8)


def sample(
    identity: int,
    *,
    size: int = 256,
    shift: tuple[int, int] = (0, 0),
    rotation_deg: float = 0.0,
    brightness: float = 1.0,
    noise: float = 3.0,
    seed: int = 0,
) -> np.ndarray:
    """One capture of ``identity`` with realistic re-capture variation."""
    image = palm_image(identity, size).astype(np.float32)

    if rotation_deg:
        rot = cv2.getRotationMatrix2D((size / 2, size / 2), rotation_deg, 1.0)
        image = cv2.warpAffine(image, rot, (size, size), borderMode=cv2.BORDER_REPLICATE)

    if shift != (0, 0):
        translate = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
        image = cv2.warpAffine(image, translate, (size, size), borderMode=cv2.BORDER_REPLICATE)

    image *= brightness
    if noise:
        image += np.random.default_rng(identity * 104729 + seed).normal(0, noise, image.shape)

    return np.clip(image, 0, 255).astype(np.uint8)


def enrollment_samples(identity: int, count: int = 3, size: int = 256) -> list[np.ndarray]:
    """A plausible set of enrollment captures of one palm."""
    variations = [
        {"shift": (0, 0), "rotation_deg": 0.0, "brightness": 1.00},
        {"shift": (2, -1), "rotation_deg": 1.5, "brightness": 0.97},
        {"shift": (-2, 2), "rotation_deg": -1.5, "brightness": 1.03},
        {"shift": (1, 1), "rotation_deg": 0.8, "brightness": 0.99},
        {"shift": (-1, -2), "rotation_deg": -0.8, "brightness": 1.01},
    ]
    return [
        sample(identity, size=size, seed=index + 1, **variations[index % len(variations)])
        for index in range(count)
    ]


def flat_print(identity: int, size: int = 256) -> np.ndarray:
    """A presentation attack: the palm re-photographed from a print.

    Reproduction destroys fine texture and flattens chroma, which is what the
    liveness heuristics look for.
    """
    image = palm_image(identity, size)
    blurred = cv2.GaussianBlur(image, (9, 9), 3.0)
    ycrcb = cv2.cvtColor(blurred, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    mean_cr = ycrcb[:, :, 1].mean()
    mean_cb = ycrcb[:, :, 2].mean()
    ycrcb[:, :, 1] = mean_cr + (ycrcb[:, :, 1] - mean_cr) * 0.05
    ycrcb[:, :, 2] = mean_cb + (ycrcb[:, :, 2] - mean_cb) * 0.05
    return cv2.cvtColor(np.clip(ycrcb, 0, 255).astype(np.uint8), cv2.COLOR_YCrCb2BGR)
