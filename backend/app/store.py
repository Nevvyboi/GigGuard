"""Where the drivers live.

A dict in memory, mirrored to a JSON file so a restart does not wipe a
driver's ledger. In production this is a database with the Investec secrets
encrypted at rest; the file is the sandbox's stand in for that, and it is
gitignored for the obvious reason.

The store also owns a lock per driver. Every route that reads a balance and
then writes it back takes that lock, which is what makes the check and record
pair safe when two taps land at once. The Express build narrowed that race
with reservations alone; here the reservation is taken under a lock, so
within one process the decision really is atomic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import engine
from .driver import Driver

log = logging.getLogger("gigguard.store")

DEMO_USER_ID = "demo"
DEMO_FLEET_ID = "bolt-cpt"
DEMO_FLEET_NAME = "Bolt Cape Town"

# A handful of drivers with feast or famine income, so both dashboards have
# something honest looking to show on a first run. Amounts are cents.
SEED_DRIVERS = [
    ("thabo", "Thabo Mokoena", [700_000, 60_000]),
    ("naledi", "Naledi Khumalo", [500_000, 150_000, 90_000]),
    ("sipho", "Sipho Dlamini", [900_000, 200_000]),
    ("aisha", "Aisha Patel", [300_000]),
]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.drivers: dict[str, Driver] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- lookup ------------------------------------------------------------

    def get(self, user_id: str) -> Driver | None:
        return self.drivers.get(user_id)

    def put(self, driver: Driver) -> Driver:
        self.drivers[driver.user_id] = driver
        return driver

    def lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def for_card(self, card_id: str | None) -> Driver | None:
        """Map a card tap to its driver.

        The card sends its own id, so one backend serves a whole fleet. A tap
        with no card id at all falls back to the demo driver, which keeps the
        simplest single card setup working. A card id we do not recognise
        returns nothing: we would rather enforce nothing than charge a
        stranger's tap against somebody else's release.
        """
        if card_id:
            for driver in self.drivers.values():
                if driver.card_id == card_id:
                    return driver
            return None
        return self.drivers.get(DEMO_USER_ID)

    def in_fleet(self, fleet_id: str) -> list[Driver]:
        return [d for d in self.drivers.values() if d.fleet_id == fleet_id]

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "drivers": [
                    json.loads(d.model_dump_json()) for d in self.drivers.values()
                ]
            }
            # Write beside the target then move, so a crash mid write cannot
            # leave a half a ledger behind.
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save store: %s", exc)

    def load(self) -> bool:
        """Returns True if a usable store was read, False if the caller should
        seed a fresh demo fleet."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        drivers = raw.get("drivers") if isinstance(raw, dict) else None
        if not isinstance(drivers, list):
            return False

        loaded: dict[str, Driver] = {}
        for item in drivers:
            try:
                driver = Driver.model_validate(item)
            except ValueError as exc:
                log.warning("skipping unreadable driver record: %s", exc)
                continue
            loaded[driver.user_id] = driver

        if not loaded:
            return False
        self.drivers = loaded
        return True

    # -- first run ---------------------------------------------------------

    def seed(self, settings) -> None:
        """Build the demo fleet. The demo driver carries the Investec sandbox
        credentials from .env so a live /poll works out of the box; the rest
        are simulated."""
        self.drivers.clear()

        self.put(
            Driver(
                user_id=DEMO_USER_ID,
                driver_name="Demo driver (you)",
                fleet_id=DEMO_FLEET_ID,
                fleet_name=DEMO_FLEET_NAME,
                card_id="card-demo",
                investec_client_id=settings.investec_client_id,
                investec_secret=settings.investec_secret,
                investec_api_key=settings.investec_api_key,
                account_id=settings.investec_account_id,
            )
        )

        for user_id, name, incomes in SEED_DRIVERS:
            driver = Driver(
                user_id=user_id,
                driver_name=name,
                fleet_id=DEMO_FLEET_ID,
                fleet_name=DEMO_FLEET_NAME,
                card_id=f"card-{user_id}",
            )
            for cents in incomes:
                engine.process_incoming_credit(driver, cents)
                engine.charge_for_payout(driver)
            self.put(driver)
