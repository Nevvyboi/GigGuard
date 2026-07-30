"""The driver record: one gig worker's ledger.

Everything money shaped in here is integer cents. Rands only ever appear at
the edge, as strings, when a human needs to read a number. If you find a
float sitting in a balance, something upstream is wrong.

The record doubles as the persistence format. `Store` dumps these straight
to JSON, so adding a field here is all it takes to have it survive a
restart. The cached OAuth token is the one exception: it is excluded from
the dump because it expires long before anyone restarts the process.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

DEFAULT_BUFFER_PERCENT = 30
DEFAULT_MIN_WEEKS = 4


class Hold(BaseModel):
    """An amount approved by /check but not yet settled by /record.

    Holds count against the weekly release the moment they are taken, which
    is what stops two taps in the same blink from both reading the same
    remaining balance. They expire on their own, because an approval whose
    matching record never arrives would otherwise wedge the cap shut.
    """

    cents: int
    at: float  # epoch seconds, when the hold was taken


class Driver(BaseModel):
    user_id: str

    # Who and where. A driver belongs to a fleet, and the fleet is the party
    # that actually pays us.
    driver_name: str = ""
    fleet_id: str = ""
    fleet_name: str = ""
    card_id: str = ""  # the programmable card, maps a tap back to this driver

    # Investec credentials, filled in by /setup or /onboard.
    investec_client_id: str = ""
    investec_secret: str = ""
    investec_api_key: str = ""
    account_id: str = ""

    # The two dials a driver controls.
    buffer_percent: int = DEFAULT_BUFFER_PERCENT  # skim this much off a credit
    min_weeks: int = DEFAULT_MIN_WEEKS  # stretch the buffer over this many weeks

    # The ledger, all cents.
    buffer_balance: int = 0
    weekly_release: int = 0
    spent_this_week: int = 0
    week_started_at: float = Field(default_factory=time.time)

    # Our own revenue: a stubbed fee per payout smoothed.
    payouts_smoothed: int = 0
    revenue_cents: int = 0

    # Polling bookkeeping. seen_txns holds the keys of credits already counted
    # so a re-poll over the same window cannot double count income.
    last_poll_date: str | None = None
    seen_txns: set[str] = Field(default_factory=set)

    # Short lived reservations, see Hold.
    holds: list[Hold] = Field(default_factory=list)

    # Cached Investec token. Never persisted; it would be stale on load.
    token: str | None = Field(default=None, exclude=True)
    token_expires_at: float = Field(default=0.0, exclude=True)

    @property
    def display_name(self) -> str:
        return self.driver_name or self.user_id

    @property
    def is_active(self) -> bool:
        """An active seat is one we bill the fleet for: a driver who has
        actually banked something. An onboarded driver who never earned is
        not a seat."""
        return self.buffer_balance > 0
