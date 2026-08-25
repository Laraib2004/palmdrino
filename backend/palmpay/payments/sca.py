"""PSD2 Strong Customer Authentication modelling.

A CORRECTION TO THE DESIGN DOCUMENT
-----------------------------------
Section 5 of the design says the palm scan "is the SCA inherence factor. It
replaces the PIN." The first half is right; the conclusion needs adjusting.

PSD2 (Art. 4(30) and the RTS on SCA) defines strong customer authentication as
two or more elements from *different* categories -- knowledge, possession,
inherence. Card + PIN qualifies: possession (the card) plus knowledge (the
PIN). A palm on its own is a single element, from a single category. It is
therefore not SCA, however good the biometric is, and no acquirer can decide
otherwise -- this is regulation, not risk appetite.

This matters because the whole "any amount, no PIN" property in section 5 rests
on the transaction being strongly authenticated. With inherence only, it is not,
and large amounts fall back on exemptions that do not stretch that far.

THE FIX FALLS OUT OF A DECISION ALREADY MADE
--------------------------------------------
The identifier hint chosen to make matching 1:small-N can double as the second
factor -- but only if it is a user-chosen secret rather than a public
identifier. So:

* hint = phone last-4 or a member number  -> public. Narrows the search, proves
  nothing, and the transaction has inherence only.
* hint = a short secret the customer chooses at enrollment -> knowledge factor.
  Combined with the palm this is two categories, and the transaction is
  properly strongly authenticated at any amount.

Both are supported here and the assessment reports which one applied. The
recommendation is the secret variant: it costs the user the same four taps and
it is the difference between a compliant flow and one that is not.

Nothing in this module is legal advice. It encodes the rules as engineering
policy so the behaviour is explicit and reviewable; an Italian
payments/privacy lawyer still has to sign off before production, as section 6.1
of the design says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..store.models import utc_now


class FactorCategory(str, Enum):
    KNOWLEDGE = "knowledge"
    POSSESSION = "possession"
    INHERENCE = "inherence"


class HintType(str, Enum):
    """What the identifier hint actually is, which decides whether it counts.

    Recorded per profile at enrollment so the SCA assessment cannot
    accidentally credit a public identifier as a knowledge factor.
    """

    PUBLIC = "public"
    SECRET = "secret"


class Exemption(str, Enum):
    NONE = "none"
    LOW_VALUE = "low_value"


# PSD2 RTS Art. 16 contactless/low-value limits, in euro cents.
LOW_VALUE_MAX_AMOUNT = 5_000  # EUR 50.00 per transaction
LOW_VALUE_CUMULATIVE_MAX = 15_000  # EUR 150.00 since last SCA
LOW_VALUE_MAX_COUNT = 5  # consecutive transactions since last SCA


@dataclass(frozen=True)
class AuthenticationFactor:
    category: FactorCategory
    method: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SCAAssessment:
    """Whether a transaction may proceed, and on what authentication basis."""

    strongly_authenticated: bool
    categories: tuple[FactorCategory, ...]
    exemption: Exemption
    may_proceed: bool
    reasons: tuple[str, ...]
    assessed_at: datetime = field(default_factory=utc_now)

    def as_dict(self) -> dict:
        return {
            "strongly_authenticated": self.strongly_authenticated,
            "categories": [c.value for c in self.categories],
            "exemption": self.exemption.value,
            "may_proceed": self.may_proceed,
            "reasons": list(self.reasons),
            "assessed_at": self.assessed_at.isoformat(),
        }


def palm_factor(distance: float, threshold: float, liveness_passed: bool) -> AuthenticationFactor:
    """Build the inherence factor from a completed biometric match.

    Carries the actual distance and the operating point so an audit log can
    show how decisive the match was, not merely that it passed.
    """
    return AuthenticationFactor(
        category=FactorCategory.INHERENCE,
        method="palm_biometric",
        evidence={
            "distance": round(distance, 5),
            "threshold": threshold,
            "liveness_passed": liveness_passed,
        },
    )


def hint_factor(hint_type: HintType) -> AuthenticationFactor | None:
    """Promote the identifier hint to a knowledge factor -- only if secret."""
    if hint_type is not HintType.SECRET:
        return None
    return AuthenticationFactor(
        category=FactorCategory.KNOWLEDGE,
        method="customer_secret_hint",
        evidence={"verified_by": "shard_lookup_and_palm_match"},
    )


@dataclass
class LowValueTracker:
    """Per-customer counters for the PSD2 low-value exemption.

    In-memory here, which is correct for the prototype and wrong for
    production: these counters are a fraud control and must be durable and
    shared across terminals, or the same customer resets their allowance by
    walking to the next till.
    """

    cumulative_minor: dict[str, int] = field(default_factory=dict)
    count: dict[str, int] = field(default_factory=dict)

    def would_qualify(self, customer_id: str, amount_minor: int) -> bool:
        if amount_minor > LOW_VALUE_MAX_AMOUNT:
            return False
        used = self.cumulative_minor.get(customer_id, 0)
        seen = self.count.get(customer_id, 0)
        return (
            used + amount_minor <= LOW_VALUE_CUMULATIVE_MAX
            and seen + 1 <= LOW_VALUE_MAX_COUNT
        )

    def record_exempt(self, customer_id: str, amount_minor: int) -> None:
        self.cumulative_minor[customer_id] = self.cumulative_minor.get(customer_id, 0) + amount_minor
        self.count[customer_id] = self.count.get(customer_id, 0) + 1

    def reset(self, customer_id: str) -> None:
        """Called after a strongly authenticated transaction clears the counters."""
        self.cumulative_minor.pop(customer_id, None)
        self.count.pop(customer_id, None)


def assess(
    factors: list[AuthenticationFactor],
    *,
    customer_id: str,
    amount_minor: int,
    tracker: LowValueTracker | None = None,
) -> SCAAssessment:
    """Decide whether a transaction is strongly authenticated or exempt."""
    categories = tuple(sorted({f.category for f in factors}, key=lambda c: c.value))
    reasons: list[str] = []

    # Inherence is only credible if the biometric actually passed liveness. A
    # spoofed palm is not an authentication element at all.
    for factor in factors:
        if factor.category is FactorCategory.INHERENCE:
            if not factor.evidence.get("liveness_passed", False):
                reasons.append("inherence_rejected_liveness_failed")
                categories = tuple(c for c in categories if c is not FactorCategory.INHERENCE)
            break

    strong = len(categories) >= 2
    if strong:
        reasons.append("sca_satisfied_two_categories")
        return SCAAssessment(
            strongly_authenticated=True,
            categories=categories,
            exemption=Exemption.NONE,
            may_proceed=True,
            reasons=tuple(reasons),
        )

    if categories:
        reasons.append(f"single_category_only:{categories[0].value}")
    else:
        reasons.append("no_valid_authentication_factor")

    # Not SCA. The only route left is the low-value exemption, which is
    # narrow by design.
    if categories and tracker is not None and tracker.would_qualify(customer_id, amount_minor):
        reasons.append("low_value_exemption_applied")
        return SCAAssessment(
            strongly_authenticated=False,
            categories=categories,
            exemption=Exemption.LOW_VALUE,
            may_proceed=True,
            reasons=tuple(reasons),
        )

    reasons.append("sca_required_but_not_satisfied")
    return SCAAssessment(
        strongly_authenticated=False,
        categories=categories,
        exemption=Exemption.NONE,
        may_proceed=False,
        reasons=tuple(reasons),
    )
