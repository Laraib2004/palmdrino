"""FAR/FRR benchmark and threshold calibration.

Produces the number that decides whether this system is safe to charge money
with: the match threshold. Run it before changing ``PALMPAY_MATCH_THRESHOLD``,
and run it again after any change to capture, features or matching.

Usage
-----
Synthetic (no data collection, proves the harness works)::

    py -3.13 scripts/benchmark.py --identities 60 --samples 5

Real palmprints (what actually matters)::

    py -3.13 scripts/benchmark.py --dataset path/to/dataset

    dataset/
      person_001/  img1.jpg img2.jpg ...
      person_002/  ...

READ THIS BEFORE QUOTING ANY NUMBER FROM THE SYNTHETIC MODE
-----------------------------------------------------------
Synthetic palms are drawn from an independent random process per identity, so
impostor pairs are far more separable than real human palms, which share
anatomical structure. Synthetic FAR is therefore optimistic by a wide and
unknown margin. It tells you the pipeline is wired up correctly. It tells you
nothing about how the system will behave on real hands, and it must never be
used to justify an operating point. Use a public palmprint dataset
(CASIA-Palmprint, IITD, PolyU) for that, and a properly consented collection
before production.
"""

from __future__ import annotations

import argparse
import itertools
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palmpay.palmprint.registry import get_engine  # noqa: E402
from palmpay.palmprint.types import Modality, Template  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


@dataclass
class Scores:
    genuine: np.ndarray
    impostor: np.ndarray


def load_dataset(root: Path) -> dict[str, list[np.ndarray]]:
    """Load images grouped by identity from one subdirectory per person."""
    identities: dict[str, list[np.ndarray]] = {}
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        images: list[np.ndarray] = []
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                images.append(image)
        if len(images) >= 2:
            identities[directory.name] = images
    if not identities:
        raise SystemExit(f"no usable identities found under {root}")
    return identities


def load_synthetic(count: int, samples: int, size: int) -> dict[str, list[np.ndarray]]:
    from tests.synthetic import sample as synth_sample

    variations = [
        {"shift": (0, 0), "rotation_deg": 0.0, "brightness": 1.00},
        {"shift": (2, -1), "rotation_deg": 1.5, "brightness": 0.97},
        {"shift": (-2, 2), "rotation_deg": -1.5, "brightness": 1.03},
        {"shift": (3, 1), "rotation_deg": 2.5, "brightness": 0.94},
        {"shift": (-1, -3), "rotation_deg": -2.0, "brightness": 1.06},
        {"shift": (1, 2), "rotation_deg": 0.5, "brightness": 1.00},
    ]
    return {
        f"synthetic_{identity:04d}": [
            synth_sample(
                identity, size=size, seed=index + 1, **variations[index % len(variations)]
            )
            for index in range(samples)
        ]
        for identity in range(1, count + 1)
    }


def extract_templates(
    identities: dict[str, list[np.ndarray]], engine, verbose: bool = True
) -> dict[str, list[Template]]:
    templates: dict[str, list[Template]] = {}
    skipped = 0
    for name, images in identities.items():
        produced: list[Template] = []
        for image in images:
            roi = engine.region_extractor.locate(image)
            if roi is None:
                skipped += 1
                continue
            produced.append(engine.feature_extractor.extract(roi))
        if len(produced) >= 2:
            templates[name] = produced
        else:
            skipped += len(images) - len(produced)
    if verbose and skipped:
        print(f"  note: {skipped} image(s) produced no usable ROI")
    return templates


def compute_scores(
    templates: dict[str, list[Template]], engine, max_impostor_pairs: int, seed: int
) -> Scores:
    genuine: list[float] = []
    for samples in templates.values():
        for a, b in itertools.combinations(samples, 2):
            genuine.append(engine.matcher.distance(a, b))

    names = list(templates)
    cross_pairs = [
        (left, right) for left, right in itertools.combinations(names, 2)
    ]
    rng = random.Random(seed)
    rng.shuffle(cross_pairs)

    impostor: list[float] = []
    for left, right in cross_pairs:
        # One comparison per identity pair keeps the sample balanced across
        # identities rather than over-weighting whoever has the most images.
        impostor.append(engine.matcher.distance(templates[left][0], templates[right][0]))
        if len(impostor) >= max_impostor_pairs:
            break

    return Scores(genuine=np.array(genuine), impostor=np.array(impostor))


def rates_at(scores: Scores, threshold: float) -> tuple[float, float]:
    """(FAR, FRR) at a threshold. Lower distance = more similar."""
    far = float((scores.impostor <= threshold).mean()) if scores.impostor.size else 0.0
    frr = float((scores.genuine > threshold).mean()) if scores.genuine.size else 0.0
    return far, frr


def find_eer(scores: Scores, grid: np.ndarray) -> tuple[float, float]:
    """Equal error rate and the threshold where it occurs."""
    best = (1.0, 0.0, 1.0)
    for threshold in grid:
        far, frr = rates_at(scores, float(threshold))
        gap = abs(far - frr)
        if gap < best[0]:
            best = (gap, float(threshold), (far + frr) / 2.0)
    return best[2], best[1]


def threshold_for_far(scores: Scores, target_far: float, grid: np.ndarray) -> float | None:
    """Highest (most permissive) threshold still meeting a FAR target."""
    chosen: float | None = None
    for threshold in grid:
        far, _ = rates_at(scores, float(threshold))
        if far <= target_far:
            chosen = float(threshold)
        else:
            break
    return chosen


def identification_far(single_far: float, shard_size: int) -> float:
    """Per-transaction false-accept probability across a candidate shard.

    Every additional candidate is another chance to be wrongly matched, so the
    risk compounds with shard size. This is the reason the design narrows to
    1:small-N instead of searching every enrolled palm.
    """
    if shard_size <= 1:
        return 0.0
    return 1.0 - (1.0 - single_far) ** (shard_size - 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, help="directory with one subfolder per identity")
    parser.add_argument("--identities", type=int, default=60, help="synthetic identity count")
    parser.add_argument("--samples", type=int, default=5, help="synthetic samples per identity")
    parser.add_argument("--size", type=int, default=256, help="synthetic image size")
    parser.add_argument("--max-impostor-pairs", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()

    engine = get_engine(Modality.PALM_PRINT_RGB)
    print(f"engine: {engine.engine_id}")

    if args.dataset:
        print(f"loading dataset from {args.dataset}")
        identities = load_dataset(args.dataset)
        synthetic = False
    else:
        print(f"generating {args.identities} synthetic identities x {args.samples} samples")
        identities = load_synthetic(args.identities, args.samples, args.size)
        synthetic = True

    print(f"identities: {len(identities)}")
    templates = extract_templates(identities, engine)
    print(f"usable identities: {len(templates)}")

    scores = compute_scores(templates, engine, args.max_impostor_pairs, args.seed)
    if scores.genuine.size == 0 or scores.impostor.size == 0:
        raise SystemExit("not enough data to compute both genuine and impostor scores")

    print()
    print(f"genuine pairs : {scores.genuine.size}")
    print(f"impostor pairs: {scores.impostor.size}")
    print(
        f"genuine  distance  mean={scores.genuine.mean():.4f} "
        f"p95={np.percentile(scores.genuine, 95):.4f} max={scores.genuine.max():.4f}"
    )
    print(
        f"impostor distance  mean={scores.impostor.mean():.4f} "
        f"p05={np.percentile(scores.impostor, 5):.4f} min={scores.impostor.min():.4f}"
    )

    grid = np.arange(0.05, 0.60, 0.002)

    print()
    print("threshold    FAR        FRR")
    print("-" * 32)
    for threshold in np.arange(0.20, 0.46, 0.02):
        far, frr = rates_at(scores, float(threshold))
        print(f"  {threshold:.3f}    {far:8.5f}   {frr:8.5f}")

    eer, eer_threshold = find_eer(scores, grid)
    print()
    print(f"EER: {eer:.5f} at threshold {eer_threshold:.3f}")

    print()
    print("operating points")
    print("-" * 60)
    resolution = 1.0 / scores.impostor.size
    for target in (1e-2, 1e-3, 1e-4):
        if target < resolution:
            print(
                f"  FAR <= {target:<8g} not measurable: needs >= {int(1/target)} "
                f"impostor pairs, have {scores.impostor.size}"
            )
            continue
        threshold = threshold_for_far(scores, target, grid)
        if threshold is None:
            print(f"  FAR <= {target:<8g} unreachable on this data")
            continue
        far, frr = rates_at(scores, threshold)
        print(
            f"  FAR <= {target:<8g} threshold={threshold:.3f}  "
            f"measured FAR={far:.5f}  FRR={frr:.5f}"
        )

    print()
    print("per-transaction false accept vs shard size (at FAR <= 1e-3 operating point)")
    print("-" * 60)
    reference = threshold_for_far(scores, 1e-3, grid)
    if reference is not None:
        single_far, _ = rates_at(scores, reference)
        # Floor the estimate at the measurement resolution: a measured zero
        # means "below what this sample can detect", not "impossible".
        effective = max(single_far, resolution)
        for shard in (8, 16, 32, 64):
            print(
                f"  shard={shard:3d}  P(false accept) ~ {identification_far(effective, shard):.5f}"
            )
        print(f"  (single-comparison FAR floored at measurement resolution {resolution:.2e})")

    if synthetic:
        print()
        print("=" * 72)
        print("SYNTHETIC DATA. These numbers validate the pipeline, not the biometric.")
        print("Impostor separation is optimistic because synthetic identities are")
        print("statistically independent while real palms are not. Do not set a")
        print("production threshold from this run -- use a real palmprint dataset.")
        print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
