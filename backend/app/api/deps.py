"""Shared FastAPI dependencies: localhost token auth and vault gating."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.state import VaultState, state


async def verify_token(
    x_mysqlrunner_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject requests that don't carry the per-launch shared secret.

    When no token was injected (standalone development) the check is skipped.
    """
    if not settings.token:
        return
    if x_mysqlrunner_token != settings.token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad or missing token",
        )


def require_unlocked() -> VaultState:
    """Dependency for endpoints that need credentials; 423 when locked."""
    if not state.is_unlocked:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="the vault is locked; unlock it with the master password first",
        )
    return state
