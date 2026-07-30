"""The HTTP surface: auth, the card hooks, the dashboards, the fleet bill.

These run the real app through the real routes. No Investec call is made
anywhere in here; /poll is the only route that would reach the bank and it is
covered separately with a stubbed client.
"""

from __future__ import annotations

import json

from app import engine
from app.config import get_settings
from app.store import Store

CARD = "card-demo"


def test_root_reports_the_service_and_its_driver_count(client):
    body = client.get("/").json()

    assert body["ok"] is True
    assert body["service"] == "gigguard"
    assert body["drivers"] == 5  # the seeded demo fleet
    assert body["docs"] == "/docs"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_data_routes_fail_closed_without_the_key(client):
    response = client.get("/status/demo", headers={"x-api-key": ""})

    assert response.status_code == 401


def test_data_routes_fail_closed_with_the_wrong_key(client):
    response = client.get("/status/demo", headers={"x-api-key": "wrong"})

    assert response.status_code == 401


def test_the_card_hook_fails_open_on_a_bad_key(client):
    """A wrong key must never brick somebody's only card. The status says no,
    the body still approves, and the card code honours either one."""
    response = client.post(
        "/check", json={"amount": 100_000, "cardId": CARD}, headers={"x-api-key": "wrong"}
    )

    assert response.status_code == 401
    assert response.json()["approved"] is True


def test_an_unknown_driver_is_a_404_not_a_crash(client):
    assert client.get("/status/nobody").status_code == 404


# ---------------------------------------------------------------------------
# the driver's journey, end to end
# ---------------------------------------------------------------------------


def test_a_payout_lands_then_the_card_enforces_the_week(client):
    income = client.post(
        "/simulate/income", json={"userId": "demo", "amountRands": 8000}
    ).json()

    assert income["skimmedToBuffer"] == "2400.00"
    assert income["spendable"] == "5600.00"
    assert income["weeklyRelease"] == "600.00"
    assert income["feeCharged"] == "1.50"

    status = client.get("/status/demo").json()
    assert status["bufferBalance"] == "2400.00"
    assert status["remainingThisWeek"] == "600.00"
    assert status["runwayWeeks"] == "4.0"

    # R200 at the till, approved, and the after hook settles it
    approved = client.post(
        "/check", json={"amount": 20_000, "merchant": "Pick n Pay", "cardId": CARD}
    ).json()
    assert approved["approved"] is True
    assert approved["merchant"] == "Pick n Pay"

    recorded = client.post(
        "/record",
        json={"type": "DEBIT", "amount": 20_000, "status": "APPROVED", "cardId": CARD},
    ).json()
    assert recorded["recorded"] is True
    assert recorded["spentThisWeek"] == "200.00"

    assert client.get("/status/demo").json()["remainingThisWeek"] == "400.00"

    # R500 is more than the R400 left, so the card says no
    declined = client.post(
        "/check", json={"amount": 50_000, "merchant": "Game", "cardId": CARD}
    ).json()
    assert declined["approved"] is False
    assert "R400.00" in declined["reason"]

    # and the decline moved nothing
    assert client.get("/status/demo").json()["spentThisWeek"] == "200.00"


def test_a_tap_on_a_card_we_do_not_know_is_approved_but_not_enforced(client):
    """Fail open, but never charge a stranger's tap against another driver's
    release."""
    before = client.get("/status/demo").json()

    response = client.post(
        "/check", json={"amount": 99_999_999, "cardId": "card-of-a-stranger"}
    ).json()

    assert response["approved"] is True
    assert "not enforced" in response["reason"]
    assert client.get("/status/demo").json()["spentThisWeek"] == before["spentThisWeek"]


def test_the_after_hook_ignores_anything_that_is_not_an_approved_debit(client):
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 8000})

    for payload in (
        {"type": "DEBIT", "amount": 30_000, "status": "DECLINED", "cardId": CARD},
        {"type": "CREDIT", "amount": 30_000, "status": "APPROVED", "cardId": CARD},
        {"amount": 30_000, "cardId": CARD},
    ):
        body = client.post("/record", json=payload).json()
        assert body["recorded"] is False

    assert client.get("/status/demo").json()["spentThisWeek"] == "0.00"


def test_amount_in_cents_and_amount_in_rands_agree(client):
    """Sending rands where cents belong was a real bug: a judge poking the API
    by hand would hit it. Both spellings now mean the same money."""
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 8000})

    by_cents = client.post("/check", json={"amount": 70_000, "cardId": CARD}).json()
    by_rands = client.post("/check", json={"amountRands": 700, "cardId": CARD}).json()

    assert by_cents["approved"] is False
    assert by_rands["approved"] is False
    assert "700.00" in by_cents["reason"]
    assert "700.00" in by_rands["reason"]


def test_two_taps_before_either_settles_cannot_both_clear_the_release(client):
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 8000})

    first = client.post("/check", json={"amount": 50_000, "cardId": CARD}).json()
    second = client.post("/check", json={"amount": 50_000, "cardId": CARD}).json()

    assert first["approved"] is True
    assert second["approved"] is False


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_setup_clamps_the_dials_instead_of_rejecting_them(client):
    body = client.post(
        "/setup", json={"userId": "demo", "bufferPercent": 900, "minWeeks": 99}
    ).json()

    assert body["bufferPercent"] == 60
    assert body["minWeeks"] == 12


def test_setup_leaves_out_fields_alone(client):
    client.post("/setup", json={"userId": "demo", "bufferPercent": 50, "minWeeks": 6})
    client.post("/setup", json={"userId": "demo", "bufferPercent": 40})

    status = client.get("/status/demo").json()
    assert status["bufferPercent"] == 40
    assert status["minWeeks"] == 6


def test_changing_the_runway_recomputes_the_weekly_release(client):
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 8000})
    assert client.get("/status/demo").json()["weeklyRelease"] == "600.00"

    client.post("/setup", json={"userId": "demo", "minWeeks": 8})

    assert client.get("/status/demo").json()["weeklyRelease"] == "300.00"


# ---------------------------------------------------------------------------
# the partner console
# ---------------------------------------------------------------------------


def test_the_fleet_bill_is_seats_plus_smoothing_fees(client):
    fleet = client.get("/fleet/bolt-cpt").json()

    # four seeded earners plus the demo driver, who has not earned yet
    assert fleet["driverCount"] == 5
    assert fleet["activeDrivers"] == 4
    assert fleet["billing"]["seatPriceRands"] == "25.00"
    assert fleet["billing"]["seatRevenueRands"] == "100.00"
    assert fleet["billing"]["smoothingRevenueRands"] == "12.00"  # 8 payouts at R1.50
    assert fleet["billing"]["monthlyTotalRands"] == "112.00"


def test_onboarding_a_driver_adds_a_row_and_a_seat_once_they_earn(client):
    onboarded = client.post(
        "/onboard",
        json={
            "driverName": "Lerato Mthembu",
            "cardId": "card-lerato",
            "fleetId": "bolt-cpt",
        },
    ).json()
    assert onboarded["userId"] == "lerato-mthembu"

    fleet = client.get("/fleet/bolt-cpt").json()
    assert fleet["driverCount"] == 6
    assert fleet["activeDrivers"] == 4  # onboarded is not yet a seat

    client.post(
        "/simulate/income", json={"userId": "lerato-mthembu", "amountRands": 4000}
    )

    fleet = client.get("/fleet/bolt-cpt").json()
    assert fleet["activeDrivers"] == 5
    assert fleet["billing"]["seatRevenueRands"] == "125.00"


def test_onboarding_the_same_driver_twice_updates_rather_than_duplicates(client):
    for _ in range(2):
        client.post("/onboard", json={"driverName": "Lerato Mthembu"})

    fleet = client.get("/fleet/bolt-cpt").json()
    ids = [d["userId"] for d in fleet["drivers"]]

    assert ids.count("lerato-mthembu") == 1


def test_onboarding_needs_a_name_or_an_id(client):
    assert client.post("/onboard", json={}).status_code == 400


def test_a_cards_tap_reaches_its_own_driver_not_the_demo_one(client):
    client.post("/simulate/income", json={"userId": "thabo", "amountRands": 1000})

    before = client.get("/status/demo").json()["spentThisWeek"]
    client.post("/check", json={"amount": 100, "cardId": "card-thabo"})
    client.post(
        "/record",
        json={
            "type": "DEBIT",
            "amount": 100,
            "status": "APPROVED",
            "cardId": "card-thabo",
        },
    )

    assert client.get("/status/thabo").json()["spentThisWeek"] == "1.00"
    assert client.get("/status/demo").json()["spentThisWeek"] == before


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_a_ledger_survives_a_restart(client):
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 8000})
    before = client.get("/status/demo").json()

    path = get_settings().store_path
    reloaded = Store(path)

    assert reloaded.load() is True
    assert engine.rands(reloaded.get("demo").buffer_balance) == before["bufferBalance"]
    assert reloaded.get("demo").payouts_smoothed == 1


def test_the_store_file_never_contains_a_cached_token(client, store_path):
    """Tokens expire in minutes. Writing one to disk is noise at best and a
    leaked credential at worst."""
    client.post("/simulate/income", json={"userId": "demo", "amountRands": 100})

    raw = json.loads(get_settings().store_path.read_text())

    assert all("token" not in d for d in raw["drivers"])


def test_a_corrupt_store_file_seeds_a_fresh_fleet_rather_than_crashing(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{ this is not json")

    store = Store(path)

    assert store.load() is False
