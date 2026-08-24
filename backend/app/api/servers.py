"""Connection profile CRUD, plus the encrypted .mrx export/import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import require_unlocked, verify_token
from mysql_runner.storage.models import (
    DEFAULT_PORTS,
    AuthType,
    ConnectionKind,
    Environment,
    ServerProfile,
)
from mysql_runner.storage.portable import (
    PortableError,
    export_profiles,
    import_profiles,
)

router = APIRouter(
    prefix="/servers", tags=["servers"], dependencies=[Depends(verify_token)]
)


class ProfileIn(BaseModel):
    """Everything the Add/Edit form can send. Absent fields keep their default."""

    label: str
    kind: ConnectionKind = ConnectionKind.PHPMYADMIN
    url: str = ""
    username: str = ""
    password: str = ""
    auth_type: AuthType = AuthType.AUTO
    group: str = ""
    environment: Environment = Environment.NONE
    startup_script: str = ""
    host: str = ""
    port: int = 0
    database: str = ""
    remote_dir: str = ""
    local_dir: str = ""
    private_key_path: str = ""
    passive: bool = True

    def to_profile(self, profile_id: str | None = None) -> ServerProfile:
        data = self.model_dump()
        if profile_id:
            data["id"] = profile_id
        return ServerProfile(**data)


class ExportRequest(BaseModel):
    passphrase: str
    path: str


class ImportRequest(BaseModel):
    passphrase: str
    path: str


def _serialize(profile: ServerProfile, *, reveal_password: bool = False) -> dict:
    """Profile as JSON. The password is withheld unless explicitly asked for."""
    data = profile.to_dict()
    data["target"] = profile.describe_target()
    data["effective_port"] = profile.effective_port
    data["is_transfer"] = profile.kind.is_transfer
    if not reveal_password:
        # The list view never needs the secret; keep it out of the webview.
        data["password"] = ""
        data["has_password"] = bool(profile.password)
    return data


@router.get("")
def list_servers(state=Depends(require_unlocked)) -> dict:
    profiles = state.store().all()
    groups: dict[str, list[dict]] = {}
    for profile in profiles:
        groups.setdefault(profile.group.strip() or "Ungrouped", []).append(
            _serialize(profile)
        )
    return {
        "servers": [_serialize(p) for p in profiles],
        "groups": [
            {"name": name, "servers": items} for name, items in sorted(groups.items())
        ],
        "default_ports": {kind.value: port for kind, port in DEFAULT_PORTS.items()},
    }


@router.get("/{profile_id}")
def read_server(profile_id: str, state=Depends(require_unlocked)) -> dict:
    profile = state.store().get(profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    # The edit form needs the stored password to round-trip it unchanged.
    return _serialize(profile, reveal_password=True)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_server(payload: ProfileIn, state=Depends(require_unlocked)) -> dict:
    profile = payload.to_profile()
    state.store().add(profile)
    return _serialize(profile)


@router.put("/{profile_id}")
def update_server(
    profile_id: str, payload: ProfileIn, state=Depends(require_unlocked)
) -> dict:
    store = state.store()
    if store.get(profile_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    profile = payload.to_profile(profile_id)
    store.update(profile)
    return _serialize(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(profile_id: str, state=Depends(require_unlocked)) -> None:
    store = state.store()
    if store.get(profile_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    store.delete(profile_id)


@router.post("/export")
def export(request: ExportRequest, state=Depends(require_unlocked)) -> dict:
    profiles = state.store().all()
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="no servers saved yet"
        )
    try:
        export_profiles(profiles, request.passphrase, request.path)
    except (PortableError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"exported": len(profiles), "path": request.path}


@router.post("/import")
def import_bundle(request: ImportRequest, state=Depends(require_unlocked)) -> dict:
    try:
        profiles = import_profiles(request.path, request.passphrase)
    except (PortableError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    count = state.store().add_many(profiles)
    return {"imported": count}
