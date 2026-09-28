"""Money helpers: exact Decimal parsing to integer cents, and a
largest-remainder split so proportional allocations always reconcile
exactly to the cent, using only integer arithmetic (no floats)."""

from __future__ import annotations

from decimal import Decimal


def parse_cents(amount: str) -> int:
    """Parse a decimal amount string (e.g. "42.99") into integer cents.

    Raises if the amount isn't a whole number of cents, rather than
    silently truncating.
    """
    value = Decimal(amount) * 100
    if value != value.to_integral_value():
        raise ValueError(f"amount {amount!r} is not a whole number of cents")
    return int(value)


def largest_remainder_split(total_cents: int, weights: list[int]) -> list[int]:
    """Split total_cents proportionally to weights (same order, same length
    as the result). Reconciles exactly to total_cents. Ties in the
    remainder are broken by ascending index, so the split is deterministic
    and stable across re-runs.
    """
    if not weights:
        return []
    if total_cents < 0:
        raise ValueError("total_cents must be non-negative")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")

    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("weights must sum to a positive number")

    floors = []
    remainders = []
    for w in weights:
        product = total_cents * w
        floors.append(product // weight_sum)
        remainders.append(product % weight_sum)

    shares = list(floors)
    leftover = total_cents - sum(floors)

    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1

    assert sum(shares) == total_cents
    return shares
