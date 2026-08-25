"""End-to-end walkthrough of the whole system, using synthetic palms.

Runs the complete journey in-process -- no server, no Android device, no
credentials -- so the design can be inspected end to end in about five seconds::

    py -3.13 scripts/demo.py

It deliberately shows the refusals as well as the happy path. The refusals are
where most of the design lives.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from palmpay.config import Settings  # noqa: E402
from palmpay.payments.gateway import CardDetails  # noqa: E402
from palmpay.payments.nexi_mock import TEST_CARDS  # noqa: E402
from palmpay.payments.sca import HintType  # noqa: E402
from palmpay.services.container import ServiceContainer  # noqa: E402
from palmpay.services.enrollment import ConsentGrant, EnrollmentError  # noqa: E402
from palmpay.services.payment import PaymentDeclined  # noqa: E402
from tests.synthetic import enrollment_samples, flat_print, sample  # noqa: E402

CONSENT = ConsentGrant(
    granted=True,
    purposes=("biometric_processing", "payment_execution"),
    policy_version="2026-01-v1",
    evidence_text="I consent to the processing of my palm biometric data.",
)

CUSTOMERS = [
    (1, "Maria Rossi", "4821", TEST_CARDS["visa_ok"]),
    (2, "Luca Bianchi", "4821", TEST_CARDS["mastercard_ok"]),
    (3, "Sofia Conti", "4821", TEST_CARDS["mastercard_2series_ok"]),
    (4, "Marco Greco", "7310", TEST_CARDS["visa_ok"]),
]


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def euros(minor: int) -> str:
    return f"EUR {minor / 100:,.2f}"


def main() -> int:
    settings = Settings(
        data_dir=Path(tempfile.mkdtemp(prefix="palmdrino_demo_")),
        shard_pepper="demo-pepper-not-for-production",
    )
    services = ServiceContainer.build(settings)
    print(f"engine   : {services.engine.engine_id}")
    print(f"threshold: {services.engine.matcher.threshold}")
    print(f"gateway  : {services.gateway.name}")
    print(f"data dir : {settings.data_dir}")

    heading("1. Enrollment (one time per customer)")
    enrolled = {}
    for identity, name, hint, pan in CUSTOMERS:
        result = services.enrollment.enroll(
            frames=enrollment_samples(identity, settings.enrollment_samples),
            hint=hint,
            hint_type=HintType.SECRET,
            card=CardDetails(pan, 12, 2032, "123", name),
            pii={"name": name, "email": f"{name.split()[0].lower()}@example.it"},
            consent=CONSENT,
        )
        enrolled[identity] = result
        print(
            f"  {name:14} -> {result.customer_id}  {result.card_display:22}"
            f"  sample agreement {result.max_pairwise_distance:.3f}"
        )

    shard = services.repository.get_profile(enrolled[1].customer_id).shard
    print(f"\n  Three customers share pay code 4821; that shard holds "
          f"{services.repository.count_in_shard(shard)} palms.")
    print("  Identification compares against those three, not the whole database.")

    heading("2. Paying by palm")
    outcome = services.payment.pay(
        frame=sample(1, shift=(2, -3), rotation_deg=2.0, brightness=0.97, seed=44),
        hint="4821",
        amount_minor=24_990,
        currency="EUR",
        merchant_id="mrc_bar_roma",
    )
    print(f"  {euros(outcome.authorization.amount_minor)} on {outcome.card_display}")
    print(f"  matched {outcome.customer_id} at distance {outcome.distance:.4f}")
    print(f"  runner-up was {outcome.margin:.4f} further away")
    print(f"  compared {outcome.candidates_considered} candidates")
    print(f"  auth code {outcome.authorization.authorization_code}")

    heading("3. A large amount, still no PIN")
    big = services.payment.pay(
        frame=sample(1, seed=12),
        hint="4821",
        amount_minor=75_000,
        currency="EUR",
        merchant_id="mrc_gioielleria",
    )
    print(f"  {euros(big.authorization.amount_minor)} -> {big.authorization.status.value}")
    print(f"  factors: {' + '.join(c.value for c in big.sca.categories)}")
    print(f"  strong authentication: {big.sca.strongly_authenticated}")
    print("  The palm is inherence; the secret pay code is knowledge. Two")
    print("  categories is what PSD2 requires, so no amount cap applies.")

    heading("4. What gets refused")
    for label, kwargs in [
        ("someone who never enrolled", dict(frame=sample(99, seed=5), hint="4821")),
        ("a printed copy of an enrolled palm", dict(frame=flat_print(1), hint="4821")),
        ("the right palm with the wrong pay code", dict(frame=sample(1, seed=6), hint="0000")),
    ]:
        try:
            services.payment.pay(
                amount_minor=1_000, currency="EUR", merchant_id="mrc_1", **kwargs
            )
            print(f"  {label:38} -> CHARGED (this should not happen)")
        except PaymentDeclined as exc:
            print(f"  {label:38} -> refused ({exc.code})")

    try:
        services.enrollment.enroll(
            frames=enrollment_samples(5, 3),
            hint="5555",
            hint_type=HintType.SECRET,
            card=CardDetails(TEST_CARDS["visa_ok"], 12, 2032, "123"),
            pii={},
            consent=ConsentGrant(False, (), ""),
        )
        print(f"  {'enrollment without consent':38} -> ACCEPTED (this should not happen)")
    except EnrollmentError as exc:
        print(f"  {'enrollment without consent':38} -> refused ({exc.code})")

    heading("5. Erasure by crypto-shred")
    victim = enrolled[1].customer_id
    services.enrollment.delete_customer(victim)
    profile = services.repository.get_profile(victim)
    print(f"  {victim} status: {profile.status.value}")
    print(f"  wrapped DEK bytes remaining: {len(profile.wrapped_dek)}")
    print(f"  encrypted template bytes remaining: {len(profile.enc_template)}")
    try:
        services.payment.pay(
            frame=sample(1, seed=7),
            hint="4821",
            amount_minor=1_000,
            currency="EUR",
            merchant_id="mrc_1",
        )
        print("  the erased palm still paid (this should not happen)")
    except PaymentDeclined as exc:
        print(f"  the erased palm can no longer pay ({exc.code})")

    consents = services.repository.get_consents(victim)
    print(f"  proof of consent retained: {len(consents)} record, "
          f"policy {consents[0].policy_version}, active={consents[0].is_active}")
    print("  The key is gone, so every ciphertext under it -- including copies")
    print("  already written to backups -- is permanently unreadable. The proof")
    print("  that consent existed survives, because that is the legal record.")

    heading("6. Audit trail")
    for event in reversed(services.repository.recent_audit(limit=8)):
        who = event.customer_id or "-"
        print(f"  {event.event_type:11} {event.outcome:9} {who}")

    services.close()
    print()
    print("Payments were simulated against a mock acquirer. No money moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
