"""The Investec side: get a token, read transactions.

Two things worth knowing before you change anything here, both of which cost
real time to learn (knowledge file, notes 2 and 3):

  1. The sandbox serves the token and the data from the same host, at
     /identity/v2/oauth2/token. Production mints tokens from a different host
     entirely. Point sandbox code at the live token host and every call fails
     auth while looking, from the outside, like an empty account.

  2. The transactions API talks rands as plain numbers. The card talks cents.
     GigGuard is cents everywhere internally, so credits get multiplied by
     100 on the way in, rounded to kill float dust. Get this wrong and you
     are off by a factor of a hundred.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import httpx

from .config import Settings
from .driver import Driver

log = logging.getLogger("gigguard.investec")

# The card hook has roughly two seconds. Nothing in here runs inside that
# budget (polling is its own request), but a hung bank call should still not
# pin a worker forever.
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Refresh a little before the token actually lapses.
TOKEN_SKEW_SECONDS = 60


class InvestecError(RuntimeError):
    """Any failure talking to Investec. Callers turn this into a 502 rather
    than a 500, because the fault is upstream, not here."""


class InvestecClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def token(self, driver: Driver) -> str:
        """Client credentials grant, cached on the driver until it is nearly
        expired. Investec wants HTTP basic auth plus the x-api-key header and
        a form body asking for the accounts scope."""
        if driver.token and time.time() < driver.token_expires_at:
            return driver.token

        try:
            response = await self._client.post(
                self._settings.investec_token_url,
                auth=(driver.investec_client_id, driver.investec_secret),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "x-api-key": driver.investec_api_key,
                },
                data={"grant_type": "client_credentials", "scope": "accounts"},
            )
        except httpx.HTTPError as exc:
            raise InvestecError(f"token request failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise InvestecError(
                f"token request failed ({response.status_code}): {response.text[:300]}"
            )

        payload = response.json()
        driver.token = payload["access_token"]
        driver.token_expires_at = (
            time.time() + int(payload.get("expires_in", 1800)) - TOKEN_SKEW_SECONDS
        )
        return driver.token

    async def transactions(
        self, driver: Driver, from_date: str, to_date: str
    ) -> list[dict]:
        token = await self.token(driver)
        url = (
            f"{self._settings.investec_api_base}"
            f"/accounts/{driver.account_id}/transactions"
        )

        try:
            response = await self._client.get(
                url,
                params={"fromDate": from_date, "toDate": to_date},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise InvestecError(f"transactions request failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise InvestecError(
                f"transactions request failed ({response.status_code}): "
                f"{response.text[:300]}"
            )

        payload = response.json()
        return payload.get("data", {}).get("transactions", []) or []


def transaction_key(txn: dict) -> str:
    """A stable identity for a credit, so re-polling the same window cannot
    double count income. Prefer a real id; otherwise fall back to the fields
    that make a credit unique, running balance included, because two identical
    same day payouts are a normal thing and must not collapse into one."""
    if txn.get("uuid"):
        return str(txn["uuid"])
    parts = [
        txn.get("transactionDate") or txn.get("postingDate") or "",
        txn.get("description") or "",
        txn.get("amount"),
        txn.get("runningBalance", ""),
    ]
    return "|".join(str(p) for p in parts)


def is_credit(txn: dict) -> bool:
    return str(txn.get("type", "")).upper() == "CREDIT"


def to_cents(rand_amount: object) -> int:
    """Rands off the API to integer cents. round() rather than int() because
    a float like 152.74999 is a real thing the wire hands you."""
    return round(float(rand_amount) * 100)


def iso_today() -> str:
    return date.today().isoformat()


def iso_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
