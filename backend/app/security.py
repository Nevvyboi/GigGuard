"""Two flavours of the same shared key, because the two callers want
opposite failure modes.

    require_api_key         the data routes. Fails CLOSED with a 401, so
                            nobody reads or moves a ledger without the key.

    require_card_api_key    the card hooks (/check, /record). Fails OPEN,
                            answering approved: true, because a wrong key
                            must never brick somebody's only card at a till.

Both compare in constant time. Before any non local use, swap this single
shared secret for real per driver authentication.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from .config import get_settings


class CardAuthError(Exception):
    """Raised by the card hook dependency. The handler registered in main
    turns this into a 401 whose body still approves the spend."""


def _key_matches(sent: str | None) -> bool:
    if not sent:
        return False
    return secrets.compare_digest(sent, get_settings().gigguard_api_key)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not _key_matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )


async def require_card_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not _key_matches(x_api_key):
        raise CardAuthError()
