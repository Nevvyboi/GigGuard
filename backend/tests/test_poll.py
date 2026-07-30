"""Polling Investec for income, with the bank stubbed out.

The real thing is covered by running it against the sandbox (see the README).
What matters here is everything around the call: which rows count as income,
that re-polling a window cannot double count, that rands become cents, and
that a bank outage reads as an upstream failure rather than our bug.
"""

from __future__ import annotations

import pytest

from app.investec import InvestecError, to_cents, transaction_key
from app.main import get_investec


class FakeInvestec:
    """Stands in for the bank. Records the window it was asked for so the
    tests can assert on the date handling too."""

    def __init__(self, transactions, error: Exception | None = None):
        self._transactions = transactions
        self._error = error
        self.windows: list[tuple[str, str]] = []

    async def transactions(self, driver, from_date, to_date):
        self.windows.append((from_date, to_date))
        if self._error:
            raise self._error
        return self._transactions

    async def aclose(self):
        pass


def credit(amount, description="STANSAL", running=None, txn_type="CREDIT", uuid=None):
    row = {"type": txn_type, "amount": amount, "description": description}
    if running is not None:
        row["runningBalance"] = running
    if uuid is not None:
        row["uuid"] = uuid
    row["transactionDate"] = "2026-03-04"
    return row


@pytest.fixture
def polling(client):
    """A demo driver with credentials set, and a bank we control."""
    client.post(
        "/setup",
        json={
            "userId": "demo",
            "investecClientId": "id",
            "investecSecret": "secret",
            "investecApiKey": "key",
            "accountId": "acct",
            "bufferPercent": 30,
            "minWeeks": 4,
        },
    )

    def use(transactions, error=None):
        fake = FakeInvestec(transactions, error)
        client.app.dependency_overrides[get_investec] = lambda: fake
        return fake

    yield client, use
    client.app.dependency_overrides.clear()


def test_polling_smooths_every_credit_and_charges_a_fee_for_each(polling):
    client, use = polling
    use([credit(8000), credit(2000, "STANCOM")])

    body = client.post("/poll", json={"userId": "demo"}).json()

    assert body["creditsCounted"] == 2
    assert body["skimmedToBuffer"] == "3000.00"  # 30% of R10,000
    assert body["weeklyRelease"] == "750.00"
    assert body["feesCharged"] == "3.00"  # two payouts at R1.50


def test_polling_ignores_debits(polling):
    client, use = polling
    use([credit(8000), credit(500, "Pick n Pay", txn_type="DEBIT")])

    body = client.post("/poll", json={"userId": "demo"}).json()

    assert body["creditsCounted"] == 1
    assert body["bufferBalance"] == "2400.00"


def test_re_polling_the_same_window_does_not_count_income_twice(polling):
    client, use = polling
    rows = [credit(8000, uuid="abc-123")]
    use(rows)

    first = client.post("/poll", json={"userId": "demo"}).json()
    second = client.post("/poll", json={"userId": "demo"}).json()

    assert first["creditsCounted"] == 1
    assert second["creditsCounted"] == 0
    assert second["bufferBalance"] == "2400.00"  # unchanged


def test_two_identical_same_day_payouts_do_not_collapse_into_one(polling):
    """Same amount, same day, same description is a normal thing for a driver
    with two platforms paying out. The running balance is what tells them
    apart when the API gives us no id."""
    client, use = polling
    use(
        [
            credit(1000, "STANSAL", running=5000),
            credit(1000, "STANSAL", running=6000),
        ]
    )

    body = client.post("/poll", json={"userId": "demo"}).json()

    assert body["creditsCounted"] == 2
    assert body["bufferBalance"] == "600.00"  # 30% of R2,000


def test_the_explicit_window_is_passed_straight_through(polling):
    client, use = polling
    fake = use([])

    client.post(
        "/poll",
        json={"userId": "demo", "fromDate": "2026-03-01", "toDate": "2026-06-01"},
    )

    assert fake.windows == [("2026-03-01", "2026-06-01")]


def test_the_next_poll_starts_where_the_last_one_stopped(polling):
    client, use = polling
    fake = use([])

    client.post("/poll", json={"userId": "demo", "toDate": "2026-06-01"})
    client.post("/poll", json={"userId": "demo"})

    assert fake.windows[1][0] == "2026-06-01"


def test_a_bank_outage_is_an_upstream_failure_not_ours(polling):
    client, use = polling
    use([], error=InvestecError("token request failed (401)"))

    response = client.post("/poll", json={"userId": "demo"})

    assert response.status_code == 502
    assert "Investec" in response.json()["detail"]


def test_polling_without_credentials_asks_you_to_run_setup(client):
    response = client.post("/poll", json={"userId": "thabo"})

    assert response.status_code == 400
    assert "setup" in response.json()["detail"]


# ---------------------------------------------------------------------------
# the rands and cents boundary, the one that bites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_amount, expected_cents",
    [(8000, 800_000), (152.75, 15_275), (0.01, 1), ("1234.56", 123_456), (0, 0)],
)
def test_the_api_talks_rands_and_we_store_cents(api_amount, expected_cents):
    assert to_cents(api_amount) == expected_cents


def test_float_dust_off_the_wire_rounds_rather_than_truncates():
    """int() would turn 152.74999 into 15274 and quietly lose a cent on every
    payout. round() keeps it honest."""
    assert to_cents(152.74999999) == 15_275


def test_a_real_uuid_wins_over_the_composed_key():
    assert transaction_key({"uuid": "abc", "amount": 1}) == "abc"
    assert transaction_key({"amount": 1, "description": "x"}) != "abc"
