"""GigGuard backend.

Four jobs:

  1. Watch each driver's Investec account for incoming payouts and skim a
     slice of every credit into that driver's buffer ledger.
  2. Answer the programmable card's beforeTransaction hook with a yes or a no
     on whether a spend fits inside this week's release, fast, because the
     hook gives us about two seconds before it approves without us.
  3. Run as a fleet product: many drivers under one paying partner, with per
     seat billing and a stubbed fee per payout smoothed.
  4. Persist all of it, so a restart does not wipe somebody's ledger.

Nothing here moves a driver's own money. The buffer is a number we keep, not
an account we sweep into. The only money that changes hands is our own fee,
and in this build even that is a stub.

Routes are grouped below by who calls them: the card, the driver's dashboard,
the partner console. Interactive docs at /docs.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import engine, investec as inv, models
from .config import Settings, get_settings
from .driver import Driver
from .investec import InvestecClient, InvestecError
from .security import CardAuthError, require_api_key, require_card_api_key
from .store import DEMO_FLEET_ID, DEMO_FLEET_NAME, DEMO_USER_ID, Store

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("gigguard")

DESCRIPTION = """
Income smoothing for gig workers on Investec programmable banking.

Withhold a slice of every payout, release a fixed amount each week, and let
the card enforce that limit at the till. Every route needs the shared secret
in an `x-api-key` header. The card hooks fail open on a bad key, by design:
wrongly declining someone's only card is the worse harm.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    store = Store(settings.store_path)

    if store.load():
        log.info("loaded %d drivers from %s", len(store.drivers), store.path)
    else:
        store.seed(settings)
        store.save()
        log.info("seeded a demo fleet of %d drivers", len(store.drivers))

    app.state.settings = settings
    app.state.store = store
    app.state.investec = InvestecClient(settings)

    log.info(
        "GigGuard listening. fleet view: "
        "curl localhost:%d/fleet/%s -H 'x-api-key: <key>'",
        settings.port,
        DEMO_FLEET_ID,
    )
    try:
        yield
    finally:
        store.save()
        await app.state.investec.aclose()


app = FastAPI(
    title="GigGuard",
    description=DESCRIPTION,
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "x-api-key"],
)


@app.exception_handler(CardAuthError)
async def card_auth_handler(_request: Request, _exc: CardAuthError) -> JSONResponse:
    """A card hook with a bad key still gets an approval. The card code checks
    the status first and would fail open anyway; answering approved in the
    body too means neither side can get this wrong."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"approved": True, "reason": "bad or missing api key"},
    )


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_investec(request: Request) -> InvestecClient:
    return request.app.state.investec


def settings_dep(request: Request) -> Settings:
    return request.app.state.settings


DataAuth = Depends(require_api_key)
CardAuth = Depends(require_card_api_key)


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def status_of(driver: Driver) -> models.StatusResponse:
    remaining = driver.weekly_release - driver.spent_this_week
    return models.StatusResponse(
        user_id=driver.user_id,
        driver_name=driver.driver_name or None,
        fleet_name=driver.fleet_name or None,
        buffer_balance=engine.rands(driver.buffer_balance),
        weekly_release=engine.rands(driver.weekly_release),
        spent_this_week=engine.rands(driver.spent_this_week),
        remaining_this_week=engine.rands(remaining),
        runway_weeks=f"{engine.runway_weeks(driver):.1f}",
        buffer_percent=driver.buffer_percent,
        min_weeks=driver.min_weeks,
        week_start_date=_iso(driver.week_started_at),
    )


def fleet_summary(store: Store, fleet_id: str) -> models.FleetResponse:
    drivers = store.in_fleet(fleet_id)
    bill = engine.fleet_billing(drivers)

    return models.FleetResponse(
        fleet_id=fleet_id,
        fleet_name=next((d.fleet_name for d in drivers if d.fleet_name), fleet_id),
        driver_count=len(drivers),
        active_drivers=bill["active_drivers"],
        drivers=[
            models.FleetDriver(
                user_id=d.user_id,
                driver_name=d.display_name,
                buffer_balance=engine.rands(d.buffer_balance),
                weekly_release=engine.rands(d.weekly_release),
                spent_this_week=engine.rands(d.spent_this_week),
                remaining_this_week=engine.rands(
                    d.weekly_release - d.spent_this_week
                ),
                runway_weeks=f"{engine.runway_weeks(d):.1f}",
                payouts_smoothed=d.payouts_smoothed,
                active=d.is_active,
            )
            for d in drivers
        ],
        billing=models.Billing(
            seat_price_rands=engine.rands(engine.SEAT_PRICE_CENTS),
            active_drivers=bill["active_drivers"],
            seat_revenue_rands=engine.rands(bill["seat_revenue"]),
            smoothing_revenue_rands=engine.rands(bill["smoothing_revenue"]),
            monthly_total_rands=engine.rands(bill["total"]),
        ),
    )


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


@app.get("/", response_model=models.ServiceResponse)
async def root(store: Store = Depends(get_store)) -> models.ServiceResponse:
    return models.ServiceResponse(
        service="gigguard", ok=True, drivers=len(store.drivers), docs="/docs"
    )


# ---------------------------------------------------------------------------
# the card. Both of these fail open.
# ---------------------------------------------------------------------------


@app.post("/check", response_model=models.CheckResponse, dependencies=[CardAuth])
async def check(
    body: models.CheckRequest, store: Store = Depends(get_store)
) -> models.CheckResponse:
    """beforeTransaction. Does this spend fit inside the week's release?

    Kept deliberately cheap: one lookup, one subtraction. The hook has about
    two seconds before Investec stops waiting and approves without us.
    """
    driver = store.for_card(body.card_id)
    if driver is None:
        # A card we do not know. Approve, so we never brick a card, but
        # enforce nothing rather than charge this tap against another
        # driver's release.
        return models.CheckResponse(
            approved=True, reason="unknown card, not enforced"
        )

    cents = body.cents()

    async with store.lock(driver.user_id):
        decision = engine.decide(driver, cents)

    log.info(
        "/check %s: %s wants R%s, R%s left this week -> %s",
        driver.display_name,
        body.merchant or "card",
        engine.rands(cents),
        engine.rands(decision.remaining),
        "approved" if decision.approved else "declined",
    )

    if not decision.approved:
        return models.CheckResponse(approved=False, reason=decision.reason)

    return models.CheckResponse(
        approved=True,
        reason=decision.reason,
        merchant=body.merchant,
        reference=body.reference,
    )


@app.post("/record", response_model=models.RecordResponse, dependencies=[CardAuth])
async def record(
    body: models.RecordRequest, store: Store = Depends(get_store)
) -> models.RecordResponse:
    """afterTransaction. Only an approved debit moves the weekly ledger."""
    driver = store.for_card(body.card_id)
    if driver is None:
        return models.RecordResponse(
            recorded=False, spent_this_week="0.00", reason="unknown card"
        )

    if not body.is_approved_debit():
        return models.RecordResponse(
            recorded=False,
            spent_this_week=engine.rands(driver.spent_this_week),
            reason="not an approved debit, ledger untouched",
        )

    async with store.lock(driver.user_id):
        engine.settle(driver, round(body.amount or 0))
        store.save()

    return models.RecordResponse(
        recorded=True, spent_this_week=engine.rands(driver.spent_this_week)
    )


# ---------------------------------------------------------------------------
# the driver's dashboard
# ---------------------------------------------------------------------------


@app.get(
    "/status/{user_id}", response_model=models.StatusResponse, dependencies=[DataAuth]
)
async def get_status(
    user_id: str, store: Store = Depends(get_store)
) -> models.StatusResponse:
    driver = store.get(user_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="no such driver")

    async with store.lock(user_id):
        if engine.reset_week_if_needed(driver):
            store.save()

    return status_of(driver)


@app.post("/setup", response_model=models.SetupResponse, dependencies=[DataAuth])
async def setup(
    body: models.SetupRequest, store: Store = Depends(get_store)
) -> models.SetupResponse:
    """Save a driver's Investec credentials and dials. Anything left out keeps
    its current value, so the dashboard can nudge one slider without wiping
    the rest."""
    driver = store.get(body.user_id) or Driver(user_id=body.user_id)

    for field in (
        "investec_client_id",
        "investec_secret",
        "investec_api_key",
        "account_id",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(driver, field, value)

    if body.buffer_percent is not None:
        driver.buffer_percent = body.buffer_percent
    if body.min_weeks is not None:
        driver.min_weeks = body.min_weeks
    engine.recompute_release(driver)

    store.put(driver)
    store.save()

    return models.SetupResponse(
        user_id=driver.user_id,
        buffer_percent=driver.buffer_percent,
        min_weeks=driver.min_weeks,
        message="saved",
    )


@app.post(
    "/simulate/income",
    response_model=models.SimulateIncomeResponse,
    dependencies=[DataAuth],
)
async def simulate_income(
    body: models.SimulateIncomeRequest, store: Store = Depends(get_store)
) -> models.SimulateIncomeResponse:
    """Sandbox shortcut: pretend a payout landed, no Investec call. Charges
    the fee, because a smoothed payout is a smoothed payout."""
    user_id = body.user_id or DEMO_USER_ID
    driver = store.get(user_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="no such driver")

    cents = round(body.amount_rands * 100)

    async with store.lock(user_id):
        skimmed = engine.process_incoming_credit(driver, cents)
        fee = engine.charge_for_payout(driver)
        store.save()

    return models.SimulateIncomeResponse(
        income_rands=engine.rands(cents),
        skimmed_to_buffer=engine.rands(skimmed),
        spendable=engine.rands(cents - skimmed),
        buffer_balance=engine.rands(driver.buffer_balance),
        weekly_release=engine.rands(driver.weekly_release),
        fee_charged=engine.rands(fee),
        revenue_to_date=engine.rands(driver.revenue_cents),
    )


@app.post("/poll", response_model=models.PollResponse, dependencies=[DataAuth])
async def poll(
    body: models.PollRequest,
    store: Store = Depends(get_store),
    client: InvestecClient = Depends(get_investec),
) -> models.PollResponse:
    """Pull real transactions from Investec and smooth every credit we have
    not seen before.

    Income is polled rather than pushed: plain incoming credits do not come
    with a webhook in the sandbox, so income is detected at the next poll
    rather than the instant it lands. Fine for smoothing, not a basis for
    promising real time alerts.
    """
    user_id = body.user_id or DEMO_USER_ID
    driver = store.get(user_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="no such driver")
    if not driver.account_id or not driver.investec_client_id:
        raise HTTPException(
            status_code=400, detail="run /setup with Investec credentials first"
        )

    from_date = body.from_date or driver.last_poll_date or inv.iso_days_ago(7)
    to_date = body.to_date or inv.iso_today()

    try:
        transactions = await client.transactions(driver, from_date, to_date)
    except InvestecError as exc:
        log.warning("poll failed for %s: %s", driver.display_name, exc)
        raise HTTPException(
            status_code=502, detail=f"could not reach Investec: {exc}"
        ) from exc

    credits_counted = 0
    skimmed_total = 0
    fees_total = 0

    async with store.lock(user_id):
        for txn in transactions:
            if not inv.is_credit(txn):
                continue
            key = inv.transaction_key(txn)
            if key in driver.seen_txns:
                continue
            driver.seen_txns.add(key)

            skimmed_total += engine.process_incoming_credit(
                driver, inv.to_cents(txn.get("amount", 0))
            )
            fees_total += engine.charge_for_payout(driver)
            credits_counted += 1

        driver.last_poll_date = to_date
        store.save()

    log.info(
        "/poll %s: %d new credits, R%s to buffer",
        driver.display_name,
        credits_counted,
        engine.rands(skimmed_total),
    )

    return models.PollResponse(
        credits_counted=credits_counted,
        skimmed_to_buffer=engine.rands(skimmed_total),
        buffer_balance=engine.rands(driver.buffer_balance),
        weekly_release=engine.rands(driver.weekly_release),
        fees_charged=engine.rands(fees_total),
        revenue_to_date=engine.rands(driver.revenue_cents),
        window=models.PollWindow(from_date=from_date, to_date=to_date),
    )


# ---------------------------------------------------------------------------
# the partner console
# ---------------------------------------------------------------------------


@app.get(
    "/fleet/{fleet_id}", response_model=models.FleetResponse, dependencies=[DataAuth]
)
async def fleet(
    fleet_id: str, store: Store = Depends(get_store)
) -> models.FleetResponse:
    return fleet_summary(store, fleet_id)


@app.post("/onboard", response_model=models.OnboardResponse, dependencies=[DataAuth])
async def onboard(
    body: models.OnboardRequest, store: Store = Depends(get_store)
) -> models.OnboardResponse:
    """Add a driver to a fleet. The stub for what would be a real connect
    flow. Idempotent on the id, so onboarding the same driver twice updates
    them rather than creating a second ledger."""
    if not body.driver_name and not body.user_id:
        raise HTTPException(
            status_code=400, detail="driverName or userId is required"
        )

    user_id = slugify(body.user_id or body.driver_name or "")
    if not user_id:
        user_id = f"driver-{len(store.drivers) + 1}"

    driver = store.get(user_id) or Driver(user_id=user_id)
    if body.driver_name:
        driver.driver_name = body.driver_name
    driver.fleet_id = body.fleet_id or driver.fleet_id or DEMO_FLEET_ID
    driver.fleet_name = body.fleet_name or driver.fleet_name or DEMO_FLEET_NAME
    if body.card_id:
        driver.card_id = body.card_id
    if body.buffer_percent is not None:
        driver.buffer_percent = body.buffer_percent
    if body.min_weeks is not None:
        driver.min_weeks = body.min_weeks
    engine.recompute_release(driver)

    store.put(driver)
    store.save()

    return models.OnboardResponse(
        user_id=driver.user_id,
        driver_name=driver.display_name,
        fleet_id=driver.fleet_id,
        card_id=driver.card_id,
        message="driver onboarded",
    )
