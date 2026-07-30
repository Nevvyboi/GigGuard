"""Request and response shapes.

Field names are snake_case in Python and camelCase on the wire, so the two
dashboards and the card code carry over from the Express build with no
changes. Money leaves as a rand string, never a float, so nothing downstream
is tempted to do arithmetic on a salary.

Requests accept either spelling, which makes hand written curl forgiving.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from . import engine


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


class DialsMixin(Wire):
    """The two dials, clamped rather than rejected.

    A client sending 900 percent gets 60, not a 422. That is deliberate: the
    clamp is a guardrail on someone's money, and the sane thing to do with a
    nonsense setting is to apply the safest one, not to fail the request and
    leave the driver on whatever they had.
    """

    buffer_percent: int | None = None
    min_weeks: int | None = None

    @field_validator("buffer_percent")
    @classmethod
    def _clamp_buffer_percent(cls, value: int | None) -> int | None:
        return None if value is None else engine.clamp_buffer_percent(value)

    @field_validator("min_weeks")
    @classmethod
    def _clamp_min_weeks(cls, value: int | None) -> int | None:
        return None if value is None else engine.clamp_min_weeks(value)


class OnboardRequest(DialsMixin):
    driver_name: str | None = None
    user_id: str | None = None
    fleet_id: str | None = None
    fleet_name: str | None = None
    card_id: str | None = None


class SetupRequest(DialsMixin):
    user_id: str
    investec_client_id: str | None = None
    investec_secret: str | None = None
    investec_api_key: str | None = None
    account_id: str | None = None


class CheckRequest(Wire):
    """The card sends amount in cents, because that is what the hook hands it.
    amount_rands is accepted so a human can test with a rand figure. Sending
    rands where cents belong was a real bug once, which is why amount wins
    when both arrive rather than the two being quietly added."""

    amount: int | None = None
    amount_rands: float | None = None
    merchant: str | None = None
    reference: str | None = None
    card_id: str | None = None

    def cents(self) -> int:
        if self.amount is not None:
            return round(self.amount)
        if self.amount_rands is not None:
            return round(self.amount_rands * 100)
        return 0


class RecordRequest(Wire):
    type: str | None = None
    amount: int | None = None
    status: str | None = None
    card_id: str | None = None

    def is_approved_debit(self) -> bool:
        """Only an approved debit may move the weekly ledger. Depending on
        card settings the after hook can fire for declines and reversals too,
        and counting one of those would eat a release the driver never spent.
        """
        return (
            str(self.type or "").upper() == "DEBIT"
            and str(self.status or "").upper() == "APPROVED"
        )


class PollRequest(Wire):
    user_id: str | None = None
    from_date: str | None = None
    to_date: str | None = None


class SimulateIncomeRequest(Wire):
    user_id: str | None = None
    amount_rands: float = Field(gt=0)


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


class ServiceResponse(Wire):
    service: str
    ok: bool
    drivers: int
    docs: str


class OnboardResponse(Wire):
    user_id: str
    driver_name: str
    fleet_id: str
    card_id: str
    message: str


class SetupResponse(Wire):
    user_id: str
    buffer_percent: int
    min_weeks: int
    message: str


class StatusResponse(Wire):
    user_id: str
    driver_name: str | None
    fleet_name: str | None
    buffer_balance: str
    weekly_release: str
    spent_this_week: str
    remaining_this_week: str
    runway_weeks: str
    buffer_percent: int
    min_weeks: int
    week_start_date: str


class CheckResponse(Wire):
    approved: bool
    reason: str
    merchant: str | None = None
    reference: str | None = None


class RecordResponse(Wire):
    recorded: bool
    spent_this_week: str
    reason: str | None = None


class FleetDriver(Wire):
    user_id: str
    driver_name: str
    buffer_balance: str
    weekly_release: str
    spent_this_week: str
    remaining_this_week: str
    runway_weeks: str
    payouts_smoothed: int
    active: bool


class Billing(Wire):
    seat_price_rands: str
    active_drivers: int
    seat_revenue_rands: str
    smoothing_revenue_rands: str
    monthly_total_rands: str


class FleetResponse(Wire):
    fleet_id: str
    fleet_name: str
    driver_count: int
    active_drivers: int
    drivers: list[FleetDriver]
    billing: Billing


class PollWindow(Wire):
    from_date: str
    to_date: str


class PollResponse(Wire):
    credits_counted: int
    skimmed_to_buffer: str
    buffer_balance: str
    weekly_release: str
    fees_charged: str
    revenue_to_date: str
    window: PollWindow


class SimulateIncomeResponse(Wire):
    income_rands: str
    skimmed_to_buffer: str
    spendable: str
    buffer_balance: str
    weekly_release: str
    fee_charged: str
    revenue_to_date: str
