"""Recognition core: capture, features, matching, liveness."""

from __future__ import annotations

import numpy as np
import pytest

from palmpay.palmprint.capture import PalmPrintRegionExtractor
from palmpay.palmprint.liveness import assess_liveness
from palmpay.palmprint.registry import available_modalities, get_engine
from palmpay.palmprint.types import Modality, Template

from .synthetic import flat_print, palm_image, sample


@pytest.fixture(scope="module")
def engine():
    return get_engine(Modality.PALM_PRINT_RGB)


def template_of(engine, frame):
    roi = engine.region_extractor.locate(frame)
    assert roi is not None, "expected a palm to be located"
    return engine.feature_extractor.extract(roi)


class TestRegionExtraction:
    def test_locates_palm_via_finger_valleys(self):
        """The primary path must work without the fallback crop rescuing it."""
        strict = PalmPrintRegionExtractor(allow_fallback=False)
        located = sum(strict.locate(sample(i, seed=1)) is not None for i in range(1, 11))
        assert located == 10

    def test_roi_is_square_and_normalised(self, engine):
        roi = engine.region_extractor.locate(sample(1, seed=1))
        assert roi.image.shape == (128, 128)
        assert roi.image.dtype == np.uint8

    def test_quality_measures_the_roi_not_the_whole_frame(self, engine):
        """Regression: coverage once used the full-frame mask.

        That made a correctly framed close-up palm report ~0.29 coverage and
        fail the gate, because it was answering "how much of the picture is
        hand" rather than "how much of the ROI is palm".
        """
        roi = engine.region_extractor.locate(sample(1, seed=1))
        assert roi.quality.coverage > 0.85
        assert roi.quality.ok

    def test_rejects_empty_frame(self, engine):
        assert engine.region_extractor.locate(np.zeros((0, 0, 3), np.uint8)) is None

    def test_falls_back_for_precropped_images(self, engine):
        """Dataset images are often already cropped and have no hand outline."""
        roi = engine.region_extractor.locate(palm_image(1, hand_shape=False))
        assert roi is not None


class TestMatching:
    def test_genuine_closer_than_impostor(self, engine):
        a = template_of(engine, sample(1, seed=1))
        b = template_of(engine, sample(1, shift=(3, -2), rotation_deg=2.0, seed=2))
        c = template_of(engine, sample(2, seed=1))
        assert engine.matcher.distance(a, b) < engine.matcher.distance(a, c)

    def test_genuine_accepted_impostor_rejected(self, engine):
        a = template_of(engine, sample(7, seed=1))
        b = template_of(engine, sample(7, shift=(2, 2), rotation_deg=-1.5, brightness=0.96, seed=3))
        c = template_of(engine, sample(8, seed=1))
        assert engine.matcher.verify(a, b).matched
        assert not engine.matcher.verify(a, c).matched

    def test_identical_templates_have_zero_distance(self, engine):
        a = template_of(engine, sample(3, seed=1))
        assert engine.matcher.distance(a, a) == pytest.approx(0.0, abs=1e-9)

    def test_survives_rotation_and_brightness(self, engine):
        reference = template_of(engine, sample(5, seed=1))
        for rotation in (-4.0, -2.0, 2.0, 4.0):
            probe = template_of(
                engine, sample(5, rotation_deg=rotation, brightness=0.94, seed=9)
            )
            distance = engine.matcher.distance(reference, probe)
            assert distance < engine.matcher.threshold, f"failed at {rotation} deg: {distance}"

    def test_refuses_cross_engine_comparison(self, engine):
        """A template from another engine build must never be silently matched."""
        a = template_of(engine, sample(1, seed=1))
        foreign = Template(
            modality=Modality.PALM_VEIN_NIR,
            algorithm="some_vein_algo",
            version=1,
            codes=a.codes,
            mask=a.mask,
        )
        with pytest.raises(ValueError, match="different engines"):
            engine.matcher.distance(a, foreign)


class TestTemplateSerialization:
    def test_round_trip(self, engine):
        original = template_of(engine, sample(2, seed=1))
        restored = Template.deserialize(original.serialize())
        assert np.array_equal(restored.codes, original.codes)
        assert np.array_equal(restored.mask, original.mask)
        assert restored.engine_id == original.engine_id

    def test_rejects_foreign_blob(self):
        with pytest.raises(ValueError):
            Template.deserialize(b"not a template at all")

    def test_rejects_truncated_blob(self, engine):
        blob = template_of(engine, sample(2, seed=1)).serialize()
        with pytest.raises(ValueError):
            Template.deserialize(blob[:-100])


class TestLiveness:
    def test_accepts_live_palm(self):
        assert assess_liveness(palm_image(1)).passed

    def test_accepts_live_palm_across_identities(self):
        assert all(assess_liveness(sample(i, seed=2)).passed for i in range(1, 8))

    def test_rejects_flat_print(self):
        report = assess_liveness(flat_print(1))
        assert not report.passed
        assert report.reasons

    def test_rejects_simulated_screen_replay(self):
        import cv2

        size = 256
        base = cv2.GaussianBlur(palm_image(1, size), (5, 5), 1.5).astype(np.float32)
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        grid = 14.0 * np.sin(2 * np.pi * xx / 3.1) + 14.0 * np.sin(2 * np.pi * yy / 3.1)
        replay = np.clip(base + grid[:, :, None], 0, 255).astype(np.uint8)
        assert not assess_liveness(replay).passed


class TestRegistry:
    def test_palm_print_is_available(self):
        assert Modality.PALM_PRINT_RGB in available_modalities()

    def test_palm_vein_is_declared_but_not_implemented(self):
        """The NIR seam must exist and must fail loudly, not silently degrade."""
        assert Modality.PALM_VEIN_NIR not in available_modalities()
        with pytest.raises(NotImplementedError, match="palm_vein_nir"):
            get_engine(Modality.PALM_VEIN_NIR)
