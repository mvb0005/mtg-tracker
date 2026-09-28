from decimal import InvalidOperation

import pytest

from mtgtracker.money import largest_remainder_split, parse_cents

# --- parse_cents ---


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("42.99", 4299),
        ("86.2", 8620),  # Apple Wallet drops trailing zeros
        ("86.20", 8620),
        ("86.200", 8620),  # extra zero digits are still whole cents
        ("0", 0),
        ("0.00", 0),
        ("0.01", 1),
        ("100", 10000),
        ("1234567.89", 123456789),
        ("1e2", 10000),  # Decimal accepts exponent notation
        (" 42.99 ", 4299),  # and surrounding whitespace
        ("-5.00", -500),  # negatives parse (e.g. a refund line); callers decide validity
    ],
)
def test_parse_cents_valid(amount, expected):
    result = parse_cents(amount)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("amount", ["42.999", "0.001", "0.005", "1.2345", "-0.001"])
def test_parse_cents_rejects_fractional_cents(amount):
    with pytest.raises(ValueError, match="not a whole number of cents"):
        parse_cents(amount)


def test_parse_cents_rejects_nan():
    with pytest.raises(ValueError, match="not a whole number of cents"):
        parse_cents("NaN")


def test_parse_cents_non_numeric_raises_invalid_operation():
    # Documents current behavior: garbage raises decimal.InvalidOperation
    # (an ArithmeticError), not ValueError, so `except ValueError` won't
    # catch it.
    with pytest.raises(InvalidOperation):
        parse_cents("abc")
    with pytest.raises(InvalidOperation):
        parse_cents("")


def test_parse_cents_infinity_raises_overflow():
    # Documents current behavior: Infinity passes the whole-cents check and
    # then fails converting to int.
    with pytest.raises(OverflowError):
        parse_cents("Infinity")


# --- largest_remainder_split ---


def test_split_empty_weights_returns_empty():
    assert largest_remainder_split(100, []) == []
    # Even an otherwise-invalid total is accepted when there's nothing to split.
    assert largest_remainder_split(-1, []) == []


def test_split_exact_proportions_have_no_remainder():
    assert largest_remainder_split(100, [1, 1, 2]) == [25, 25, 50]


def test_split_matches_order_fixture_tax_allocation():
    # 822c tax over line subtotals 4299c and 3499c (the committed fixture).
    assert largest_remainder_split(822, [4299, 3499]) == [453, 369]


def test_split_tie_break_is_by_ascending_index():
    # 10 / 3 = 3 each, remainder 1; all remainders equal, so index 0 wins.
    assert largest_remainder_split(10, [1, 1, 1]) == [4, 3, 3]
    # 11 / 3: remainder 2 goes to indexes 0 and 1.
    assert largest_remainder_split(11, [1, 1, 1]) == [4, 4, 3]


def test_split_largest_remainder_wins_over_index():
    # Exact shares: 100*1/7=14.28, 100*2/7=28.57, 100*4/7=57.14.
    # Floors 14+28+57=99; the one leftover cent goes to index 1 (largest
    # fractional part .57), not index 0.
    assert largest_remainder_split(100, [1, 2, 4]) == [14, 29, 57]


def test_split_zero_weight_gets_nothing():
    assert largest_remainder_split(7, [0, 1, 0, 1]) == [0, 4, 0, 3]


def test_split_zero_total():
    assert largest_remainder_split(0, [3, 5]) == [0, 0]


def test_split_total_smaller_than_count():
    assert largest_remainder_split(2, [1, 1, 1, 1, 1]) == [1, 1, 0, 0, 0]


@pytest.mark.parametrize(
    ("total", "weights"),
    [
        (7798, [1] * 100),
        (4752, [1] * 100),
        (1, [1, 1, 1]),
        (999_999, [3, 7, 11, 13, 17]),
        (822, [4299, 3499]),
        (12345, [1, 0, 999, 2, 50]),
    ],
)
def test_split_always_reconciles(total, weights):
    shares = largest_remainder_split(total, weights)
    assert sum(shares) == total
    assert len(shares) == len(weights)
    assert all(isinstance(s, int) and s >= 0 for s in shares)
    # Each share is within one cent of its exact proportional value.
    weight_sum = sum(weights)
    for share, w in zip(shares, weights, strict=True):
        assert share * weight_sum - total * w < weight_sum
        assert total * w - share * weight_sum < weight_sum


def test_split_is_deterministic():
    assert largest_remainder_split(1000, [3, 3, 3]) == largest_remainder_split(1000, [3, 3, 3])


def test_split_rejects_negative_total():
    with pytest.raises(ValueError, match="total_cents must be non-negative"):
        largest_remainder_split(-1, [1])


def test_split_rejects_negative_weight():
    with pytest.raises(ValueError, match="weights must be non-negative"):
        largest_remainder_split(10, [1, -1, 2])


def test_split_rejects_all_zero_weights():
    with pytest.raises(ValueError, match="sum to a positive number"):
        largest_remainder_split(10, [0, 0])
    # Even for a zero total: there's no proportion to split by.
    with pytest.raises(ValueError, match="sum to a positive number"):
        largest_remainder_split(0, [0, 0])
