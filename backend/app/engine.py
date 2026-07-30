"""The buffer engine: every rule GigGuard actually enforces.

This module is deliberately dull. No framework, no network, no clock it does
not accept as an argument. That makes it the one place worth reading to know
what GigGuard does to someone's money, and it makes the test suite able to
exercise the real code instead of a copy of it.

The whole product is five lines of arithmetic:

    on each incoming credit:
        to_buffer      = floor(income_cents * buffer_percent / 100)
        spendable      = income_cents - to_buffer
        buffer_balance += to_buffer
        weekly_release = floor(buffer_balance / min_weeks)

    on each card spend:
        remaining = weekly_release - spent_this_week - held
        decline if spend_cents > remaining

Flooring everywhere means the odd cent stays parked in the buffer rather
than being handed out, which is the safe direction to round.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from .driver import Driver, Hold

# What we charge. The fee per payout is a stub where a real build would call
# Paystack; the seat price is what a fleet pays per active driver per month.
PAYOUT_FEE_CENTS = 150
SEAT_PRICE_CENTS = 2500

# How long an approved but unsettled spend counts against the release. The
# card's beforeTransaction hook has roughly a two second budget, so this is
# that plus a margin.
HOLD_TTL_SECONDS = 5.0

WEEK_SECONDS = 7 * 24 * 60 * 60

# Guardrails on the two dials, applied no matter what a client sends.
BUFFER_PERCENT_MIN, BUFFER_PERCENT_MAX = 10, 60
MIN_WEEKS_MIN, MIN_WEEKS_MAX = 1, 12


def rands(cents: int) -> str:
    """Cents to a rand string. The only place money stops being an integer,
    and it happens on the way out to a human, never on the way into a sum."""
    return f"{cents / 100:.2f}"


def clamp(value: object, low: int, high: int) -> int:
    """Pull a number into range, treating anything unparseable as the floor.
    A client sending nonsense gets the safest setting, not an exception."""
    try:
        n = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return low
    return max(low, min(high, n))


def clamp_buffer_percent(value: object) -> int:
    return clamp(value, BUFFER_PERCENT_MIN, BUFFER_PERCENT_MAX)


def clamp_min_weeks(value: object) -> int:
    return clamp(value, MIN_WEEKS_MIN, MIN_WEEKS_MAX)


def recompute_release(driver: Driver) -> int:
    """The weekly release always falls out of the buffer and the runway.
    It is never stored independently, so it cannot drift out of step."""
    driver.weekly_release = driver.buffer_balance // max(1, driver.min_weeks)
    return driver.weekly_release


def process_incoming_credit(driver: Driver, amount_cents: int) -> int:
    """A payout landed. Skim the buffer percent off it and recompute the
    weekly release off the new buffer total. Returns what went to buffer."""
    to_buffer = (amount_cents * driver.buffer_percent) // 100
    driver.buffer_balance += to_buffer
    recompute_release(driver)
    return to_buffer


def charge_for_payout(driver: Driver) -> int:
    """The one place money genuinely changes hands: our own fee. Stubbed
    where a real build would call a payment provider. We record it so the
    revenue line in the partner console is a real number, not a slide."""
    driver.payouts_smoothed += 1
    driver.revenue_cents += PAYOUT_FEE_CENTS
    return PAYOUT_FEE_CENTS


def reset_week_if_needed(driver: Driver, now: float | None = None) -> bool:
    """Lazy week reset. There is no cron. Whenever anyone touches a driver we
    check whether a full seven days have passed and, if so, start a fresh
    week from that moment. The reset lands on first use of the new week
    rather than exactly at the boundary, which nobody notices."""
    now = time.time() if now is None else now
    if now - driver.week_started_at >= WEEK_SECONDS:
        driver.spent_this_week = 0
        driver.week_started_at = now
        return True
    return False


def sweep_holds(driver: Driver, now: float | None = None) -> None:
    now = time.time() if now is None else now
    driver.holds = [h for h in driver.holds if now - h.at < HOLD_TTL_SECONDS]


def held_cents(driver: Driver, now: float | None = None) -> int:
    sweep_holds(driver, now)
    return sum(h.cents for h in driver.holds)


def remaining_cents(driver: Driver, now: float | None = None) -> int:
    """What is left to spend this week, with unsettled approvals already
    counted against it."""
    return driver.weekly_release - driver.spent_this_week - held_cents(driver, now)


@dataclass(frozen=True)
class Decision:
    approved: bool
    remaining: int  # what was left before this spend
    reason: str


def decide(driver: Driver, amount_cents: int, now: float | None = None) -> Decision:
    """The gatekeeper. This is what the card's beforeTransaction hook leans
    on. An approval also takes a hold, so the answer is only good for the
    few seconds it takes the matching /record to arrive."""
    now = time.time() if now is None else now
    reset_week_if_needed(driver, now)
    remaining = remaining_cents(driver, now)

    if amount_cents > remaining:
        return Decision(
            approved=False,
            remaining=remaining,
            reason=(
                f"Over the weekly release. R{rands(remaining)} left, "
                f"this spend is R{rands(amount_cents)}."
            ),
        )

    driver.holds.append(Hold(cents=amount_cents, at=now))
    return Decision(
        approved=True,
        remaining=remaining,
        reason=(
            f"Within the weekly release. R{rands(remaining - amount_cents)} "
            f"left after this."
        ),
    )


def settle(driver: Driver, amount_cents: int, now: float | None = None) -> None:
    """An approved debit actually went through. Move the weekly total and
    release the reservation it settles, matched by amount so a driver with
    several taps in flight releases the right one."""
    now = time.time() if now is None else now
    reset_week_if_needed(driver, now)
    driver.spent_this_week += amount_cents

    for i, hold in enumerate(driver.holds):
        if hold.cents == amount_cents:
            driver.holds.pop(i)
            return
    if driver.holds:
        driver.holds.pop(0)


def runway_weeks(driver: Driver) -> float:
    """How many weeks the buffer covers at the current release."""
    if driver.weekly_release <= 0:
        return 0.0
    return driver.buffer_balance / driver.weekly_release


def fleet_billing(drivers: Iterable[Driver]) -> dict[str, int]:
    """What a fleet owes: a seat for every driver who banked something, plus
    the smoothing fees taken across all of them."""
    drivers = list(drivers)
    active = [d for d in drivers if d.is_active]
    seat_revenue = len(active) * SEAT_PRICE_CENTS
    smoothing_revenue = sum(d.revenue_cents for d in drivers)
    return {
        "active_drivers": len(active),
        "seat_revenue": seat_revenue,
        "smoothing_revenue": smoothing_revenue,
        "total": seat_revenue + smoothing_revenue,
    }
