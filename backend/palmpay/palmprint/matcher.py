"""Template comparison and the accept/reject decision.

Distance is the mean angular difference between two orientation code maps,
normalised to [0, 1]: 0 is identical, 1 is maximally different. Only pixels
both templates marked as reliable participate.

A small translation search absorbs the residual misalignment left by ROI
localisation. Without it, a two-pixel ROI offset -- routine with a handheld
capture -- inflates the distance enough to reject a genuine user.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .types import Template


@dataclass(frozen=True)
class MatchResult:
    distance: float
    threshold: float

    @property
    def matched(self) -> bool:
        return self.distance <= self.threshold

    @property
    def confidence(self) -> float:
        """Distance mapped to a 0..1 score for display and logging.

        Not a probability. It exists so clients can show a meter and so audit
        logs record how decisive a match was; never use it as a second
        threshold.
        """
        if self.threshold <= 0:
            return 0.0
        return float(np.clip(1.0 - self.distance / (2.0 * self.threshold), 0.0, 1.0))


def _angular_distance_map(
    a_codes: np.ndarray, b_codes: np.ndarray, orientations: int
) -> np.ndarray:
    """Circular difference between two orientation code maps, normalised.

    Codes are indices on a circle of ``orientations`` steps, so index 0 and
    index ``orientations - 1`` are neighbours, not opposites. Plain absolute
    difference would wrongly call them maximally distant.
    """
    raw = np.abs(a_codes.astype(np.int16) - b_codes.astype(np.int16))
    circular = np.minimum(raw, orientations - raw)
    return circular.astype(np.float32) / (orientations / 2.0)


def _overlap_slices(shift: int, extent: int) -> tuple[slice, slice]:
    """Slices selecting the overlapping band of two arrays offset by ``shift``."""
    if shift >= 0:
        return slice(shift, extent), slice(0, extent - shift)
    return slice(0, extent + shift), slice(-shift, extent)


class CompetitiveCodeMatcher:
    """Matcher for competitive-code templates.

    ``threshold`` is the operating point: the maximum distance still accepted
    as the same palm. It is the single most consequential number in the system
    -- it *is* the false-accept rate -- and must be chosen from a benchmark
    sweep (see ``benchmark.py``), never guessed. The default below is a
    conservative starting point for the prototype only.
    """

    def __init__(
        self,
        threshold: float = 0.32,
        max_shift: int = 4,
        orientations: int = 6,
        min_overlap_ratio: float = 0.25,
    ) -> None:
        self.threshold = threshold
        self.max_shift = max_shift
        self.orientations = orientations
        self.min_overlap_ratio = min_overlap_ratio

    def _assert_comparable(self, a: Template, b: Template) -> None:
        if a.engine_id != b.engine_id:
            raise ValueError(
                f"refusing to compare templates from different engines: "
                f"{a.engine_id} vs {b.engine_id}"
            )
        if a.codes.shape != b.codes.shape:
            raise ValueError("templates have different shapes")

    def distance(self, a: Template, b: Template) -> float:
        """Best (lowest) distance over the translation search window."""
        self._assert_comparable(a, b)
        height, width = a.codes.shape
        total_pixels = height * width
        best = 1.0

        for dy in range(-self.max_shift, self.max_shift + 1):
            a_rows, b_rows = _overlap_slices(dy, height)
            for dx in range(-self.max_shift, self.max_shift + 1):
                a_cols, b_cols = _overlap_slices(dx, width)

                a_mask = a.mask[a_rows, a_cols]
                b_mask = b.mask[b_rows, b_cols]
                valid = (a_mask > 0) & (b_mask > 0)
                valid_count = int(valid.sum())

                # Too little reliable overlap for the score to mean anything.
                if valid_count < self.min_overlap_ratio * total_pixels:
                    continue

                diff = _angular_distance_map(
                    a.codes[a_rows, a_cols], b.codes[b_rows, b_cols], self.orientations
                )
                score = float(diff[valid].mean())
                if score < best:
                    best = score

        return best

    def verify(self, a: Template, b: Template) -> MatchResult:
        """1:1 verification against the configured operating point."""
        return MatchResult(distance=self.distance(a, b), threshold=self.threshold)
