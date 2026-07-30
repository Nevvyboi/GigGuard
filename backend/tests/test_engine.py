"""The money math, tested against the real engine.

The Express build kept its test suite as a hand copied version of the
backend's arithmetic, which meant the tests could pass while the server was
wrong. These import the actual functions the card hook calls, so a change to
a rule shows up here whether the author remembered the tests or not.

Every amount is in cents. R8,000 is 800_000.
"""

from __future__ import annotations

import pytest

from app import engine
from app.driver import Driver

R8000 = 800_000
R3000 = 300_000
R10000 = 1_000_000


def driver(buffer_percent: int = 30, min_weeks: int = 4, **kwargs) -> Driver:
    return Driver(
        user_id="t", buffer_percent=buffer_percent, min_weeks=min_weeks, **kwargs
    )


# ---------------------------------------------------------------------------
# withholding
# ---------------------------------------------------------------------------


def test_fresh_driver_owes_nothing_and_can_spend_nothing():
    d = driver()
    assert (d.buffer_balance, d.weekly_release, d.spent_this_week) == (0, 0, 0)
    assert engine.remaining_cents(d) == 0
    assert engine.decide(d, 1).approved is False


def test_r8000_at_30_percent_withholds_2400_and_releases_600_a_week():
    d = driver()
    to_buffer = engine.process_incoming_credit(d, R8000)

    assert to_buffer == 240_000
    assert R8000 - to_buffer == 560_000  # spendable straight away
    assert d.buffer_balance == 240_000
    assert d.weekly_release == 60_000
    assert engine.runway_weeks(d) == pytest.approx(4.0)


def test_a_second_payout_stacks_onto_the_buffer():
    d = driver()
    engine.process_incoming_credit(d, R8000)
    engine.process_incoming_credit(d, R3000)

    assert d.buffer_balance == 330_000
    assert d.weekly_release == 82_500


def test_a_zero_credit_leaves_everything_untouched():
    """Reversals show up as zero amounts. They must not move a ledger."""
    d = driver()
    engine.process_incoming_credit(d, R8000)
    before = (d.buffer_balance, d.weekly_release)

    engine.process_incoming_credit(d, 0)

    assert (d.buffer_balance, d.weekly_release) == before


def test_a_saver_on_50_percent_over_8_weeks():
    d = driver(buffer_percent=50, min_weeks=8)
    to_buffer = engine.process_incoming_credit(d, R10000)

    assert to_buffer == 500_000
    assert d.weekly_release == 62_500


def test_flooring_parks_the_odd_cent_in_the_buffer_not_in_the_release():
    """Rounding always has to favour the buffer. A driver who is owed a
    fraction of a cent gets it withheld, never released."""
    d = driver(buffer_percent=33, min_weeks=7)
    to_buffer = engine.process_incoming_credit(d, 1001)

    assert to_buffer == 330  # 330.33 floored
    assert d.weekly_release == 47  # 47.14 floored
    assert d.weekly_release * 7 <= d.buffer_balance


# ---------------------------------------------------------------------------
# the decline at the till
# ---------------------------------------------------------------------------


def test_a_spend_inside_the_release_is_approved_and_moves_the_week():
    d = driver()
    engine.process_incoming_credit(d, R8000)  # release R600

    decision = engine.decide(d, 20_000)
    engine.settle(d, 20_000)

    assert decision.approved is True
    assert d.spent_this_week == 20_000
    assert engine.remaining_cents(d) == 40_000


def test_a_spend_over_the_release_is_declined_and_the_reason_names_the_shortfall():
    d = driver()
    engine.process_incoming_credit(d, R8000)
    engine.settle(d, 20_000)  # R400 left

    decision = engine.decide(d, 50_000)

    assert decision.approved is False
    assert "R400.00" in decision.reason
    assert "R500.00" in decision.reason
    assert d.spent_this_week == 20_000  # a decline moves nothing


def test_spending_exactly_what_is_left_is_allowed():
    d = driver()
    engine.process_incoming_credit(d, R8000)
    engine.settle(d, 20_000)

    decision = engine.decide(d, 40_000)
    engine.settle(d, 40_000)

    assert decision.approved is True
    assert engine.remaining_cents(d) == 0


def test_one_cent_over_an_empty_week_is_still_over():
    d = driver()
    engine.process_incoming_credit(d, R8000)
    engine.settle(d, 60_000)  # the whole release

    assert engine.decide(d, 1).approved is False


# ---------------------------------------------------------------------------
# the gap between check and record
# ---------------------------------------------------------------------------


def test_two_taps_in_the_same_blink_cannot_both_clear_the_release():
    """The race that matters. Both checks land before either records, so
    without a reservation both would read R600 free and slip past the cap."""
    d = driver()
    engine.process_incoming_credit(d, R8000)  # release R600

    first = engine.decide(d, 50_000, now=1000.0)
    second = engine.decide(d, 50_000, now=1000.0)

    assert first.approved is True
    assert second.approved is False
    assert second.remaining == 10_000


def test_a_hold_whose_record_never_arrives_expires_instead_of_wedging_the_cap():
    d = driver()
    engine.process_incoming_credit(d, R8000)

    engine.decide(d, 50_000, now=1000.0)
    still_held = engine.decide(d, 50_000, now=1000.0 + engine.HOLD_TTL_SECONDS - 0.1)
    after_expiry = engine.decide(d, 50_000, now=1000.0 + engine.HOLD_TTL_SECONDS + 0.1)

    assert still_held.approved is False
    assert after_expiry.approved is True


def test_settling_releases_the_hold_that_matches_the_amount():
    d = driver()
    engine.process_incoming_credit(d, R8000)

    engine.decide(d, 10_000, now=1000.0)
    engine.decide(d, 20_000, now=1000.0)
    engine.settle(d, 20_000, now=1000.0)

    assert [h.cents for h in d.holds] == [10_000]
    assert d.spent_this_week == 20_000


# ---------------------------------------------------------------------------
# the week
# ---------------------------------------------------------------------------


def test_the_week_resets_lazily_once_seven_days_have_passed():
    d = driver(week_started_at=1000.0)
    engine.process_incoming_credit(d, R8000)
    engine.settle(d, 60_000, now=1000.0)

    assert engine.reset_week_if_needed(d, now=1000.0 + engine.WEEK_SECONDS - 1) is False
    assert d.spent_this_week == 60_000

    assert engine.reset_week_if_needed(d, now=1000.0 + engine.WEEK_SECONDS) is True
    assert d.spent_this_week == 0
    assert engine.remaining_cents(d, now=1000.0 + engine.WEEK_SECONDS) == 60_000


# ---------------------------------------------------------------------------
# revenue
# ---------------------------------------------------------------------------


def test_the_payment_stub_takes_r1_50_per_payout_smoothed():
    d = driver()
    for cents in (700_000, 60_000):
        engine.process_incoming_credit(d, cents)
        engine.charge_for_payout(d)

    assert d.payouts_smoothed == 2
    assert d.revenue_cents == 300


def test_a_fleet_pays_a_seat_for_every_driver_who_actually_banked_something():
    earners = []
    for cents in (700_000, 500_000, 900_000):
        d = driver()
        engine.process_incoming_credit(d, cents)
        engine.charge_for_payout(d)
        earners.append(d)
    idle = driver()  # onboarded, never earned, not a seat

    bill = engine.fleet_billing([*earners, idle])

    assert bill["active_drivers"] == 3
    assert bill["seat_revenue"] == 7_500
    assert bill["smoothing_revenue"] == 450
    assert bill["total"] == 7_950


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sent, expected",
    [(30, 30), (10, 10), (60, 60), (0, 10), (9, 10), (61, 60), (900, 60), ("nonsense", 10)],
)
def test_buffer_percent_is_clamped_whatever_a_client_sends(sent, expected):
    assert engine.clamp_buffer_percent(sent) == expected


@pytest.mark.parametrize(
    "sent, expected", [(4, 4), (1, 1), (12, 12), (0, 1), (13, 12), (None, 1)]
)
def test_min_weeks_is_clamped_whatever_a_client_sends(sent, expected):
    assert engine.clamp_min_weeks(sent) == expected


def test_rands_renders_cents_without_ever_returning_a_float():
    assert engine.rands(240_000) == "2400.00"
    assert engine.rands(1) == "0.01"
    assert engine.rands(0) == "0.00"
    assert isinstance(engine.rands(60_000), str)
