"""Vault endpoints: status, first-run creation, unlock, lock, protection mode."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import verify_token
from app.core.state import state
from mysql_runner.crypto import dpapi
from mysql_runner.crypto import vault as vault_mod

router = APIRouter(prefix="/vault", tags=["vault"], dependencies=[Depends(verify_token)])


class CreateRequest(BaseModel):
    #: Omit or leave empty to create a password-free (Windows-sealed) vault.
    password: str = ""


class UnlockRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class ProtectionRequest(BaseModel):
    mode: str
    #: Current master password, required when turning protection off.
    current_password: str = ""
    #: New master password, required when turning protection on.
    new_password: str = ""


@router.get("/status")
def read_status() -> dict:
    return {
        **state.status(),
        "windows_protection_available": dpapi.is_available(),
    }


@router.post("/create")
def create(request: CreateRequest) -> dict:
    if state.is_initialized:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a vault already exists"
        )
    if request.password and len(request.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="use at least 6 characters",
        )
    try:
        state.create(request.password or None)
    except vault_mod.VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return read_status()


@router.post("/unlock")
def unlock(request: UnlockRequest) -> dict:
    if not state.is_initialized:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="no vault has been created yet"
        )
    try:
        state.unlock_with_password(request.password)
    except vault_mod.InvalidMasterPassword as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="that master password is incorrect",
        ) from exc
    except vault_mod.VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return read_status()


@router.post("/auto-unlock")
def auto_unlock() -> dict:
    """Open the vault without a prompt when the mode allows it."""
    state.try_auto_unlock()
    return read_status()


@router.post("/lock")
def lock() -> dict:
    # Sessions hold live credentials, so they go when the vault does.
    from app.services import mysql_service, transfer_service

    mysql_service.manager.close_all()
    transfer_service.manager.close_all()
    state.lock()
    return read_status()


@router.post("/change-password")
def change_password(request: ChangePasswordRequest) -> dict:
    try:
        vault_mod.change_master_password(
            request.current_password, request.new_password
        )
    except vault_mod.InvalidMasterPassword as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="the current master password is incorrect",
        ) from exc
    except vault_mod.VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return read_status()


@router.post("/protection")
def set_protection(request: ProtectionRequest) -> dict:
    """Switch between a master password and password-free (DPAPI) mode."""
    if request.mode not in (vault_mod.PROTECTION_PASSWORD, vault_mod.PROTECTION_WINDOWS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="unknown protection mode"
        )
    try:
        if request.mode == vault_mod.PROTECTION_WINDOWS:
            vault_mod.disable_password(request.current_password)
        else:
            if len(request.new_password) < 6:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="use at least 6 characters",
                )
            vault_mod.enable_password(request.new_password)
    except vault_mod.InvalidMasterPassword as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="that master password is incorrect",
        ) from exc
    except vault_mod.VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return read_status()
