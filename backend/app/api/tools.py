"""Endpoints that belong to the app rather than to one open session.

The snippet library, connection-string and WinSCP import/export, and the list of
external terminals installed on this machine. All of it is the same code the
desktop app uses, so the two front ends stay in step.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import require_unlocked, verify_token
from mysql_runner.transfer import connstr, spawn
from mysql_runner.transfer.snippets import PLACEHOLDERS, Snippet, SnippetLibrary, render

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(verify_token)])

library = SnippetLibrary()


class SnippetPayload(BaseModel):
    id: str = ""
    name: str
    command: str
    description: str = ""
    confirm: bool = False
    tags: list[str] = []


class RenderRequest(BaseModel):
    command: str
    context: dict[str, str] = {}


class ImportRequest(BaseModel):
    #: A WinSCP.ini or a list of connection strings, by path…
    path: str = ""
    #: …or pasted straight in.
    text: str = ""
    #: Add the imported profiles to the vault rather than just previewing them.
    save: bool = False


class ExportRequest(BaseModel):
    #: "winscp" or "urls".
    format: str = "winscp"
    include_passwords: bool = False
    path: str = ""


class ParseRequest(BaseModel):
    url: str
    label: str = ""
    save: bool = False


def _snippet_dict(snippet: Snippet) -> dict:
    data = snippet.to_dict()
    data["placeholders"] = snippet.placeholders()
    return data


# ----- snippets -----------------------------------------------------------
@router.get("/snippets")
def list_snippets() -> dict:
    return {
        "snippets": [_snippet_dict(item) for item in library.all()],
        "tags": library.tags(),
        "placeholders": [{"name": name, "note": note} for name, note in PLACEHOLDERS],
        "path": library.path,
    }


@router.post("/snippets")
def save_snippet(payload: SnippetPayload) -> dict:
    snippet = Snippet(
        name=payload.name,
        command=payload.command,
        description=payload.description,
        confirm=payload.confirm,
        tags=list(payload.tags),
    )
    if payload.id:
        snippet.id = payload.id
        if not library.update(snippet):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such snippet"
            )
    else:
        library.add(snippet)
    return _snippet_dict(snippet)


@router.delete("/snippets/{snippet_id}")
def delete_snippet(snippet_id: str) -> dict:
    if not library.delete(snippet_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such snippet"
        )
    return {"ok": True}


@router.post("/snippets/restore-defaults")
def restore_defaults() -> dict:
    return {"added": library.restore_defaults()}


@router.post("/snippets/render")
def render_snippet(request: RenderRequest) -> dict:
    """Fill in the placeholders, so the UI can show exactly what will run."""
    return {"command": render(request.command, request.context)}


# ----- connection strings and WinSCP -------------------------------------
@router.post("/connections/parse")
def parse_connection(request: ParseRequest, state=Depends(require_unlocked)) -> dict:
    try:
        profile = connstr.parse_url(request.url, label=request.label)
    except connstr.ConnStrError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if request.save:
        state.store().add(profile)
    data = profile.to_dict()
    data.pop("password", None)  # never echo a credential back to the page
    return {"profile": data, "saved": request.save}


@router.post("/connections/import")
def import_connections(request: ImportRequest, state=Depends(require_unlocked)) -> dict:
    if not request.path and not request.text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="give either a path or the text to import",
        )
    try:
        if request.path:
            if not os.path.isfile(request.path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{request.path} does not exist",
                )
            result = connstr.load_any(request.path)
        elif "[Sessions\\" in request.text:
            result = connstr.parse_winscp_ini(request.text)
        else:
            profiles, problems = connstr.parse_url_list(request.text)
            result = connstr.WinScpImport(profiles=profiles, skipped=problems)
    except connstr.ConnStrError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    saved = 0
    if request.save and result.profiles:
        saved = state.store().add_many(result.profiles)
    return {
        "found": len(result.profiles),
        "saved": saved,
        "skipped": result.skipped,
        "profiles": [
            {
                "label": profile.label,
                "kind": profile.kind.value,
                "host": profile.host,
                "port": profile.effective_port,
                "username": profile.username,
                "remote_dir": profile.remote_dir,
                "has_password": bool(profile.password),
                "environment": profile.environment.value,
            }
            for profile in result.profiles
        ],
    }


@router.post("/connections/export")
def export_connections(request: ExportRequest, state=Depends(require_unlocked)) -> dict:
    profiles = [p for p in state.store().all() if p.kind.is_transfer]
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no file-transfer connections are saved",
        )
    if request.format == "urls":
        text = connstr.to_url_list(profiles, include_passwords=request.include_passwords)
    else:
        text = connstr.to_winscp_ini(
            profiles, include_passwords=request.include_passwords
        )
    written = ""
    if request.path:
        try:
            with open(request.path, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(text)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        written = request.path
    return {"count": len(profiles), "path": written, "text": text}


# ----- external terminals ------------------------------------------------
@router.get("/terminals")
def terminals() -> dict:
    return {
        "terminals": [
            {"name": found.name, "kind": found.kind.value, "path": found.executable}
            for found in spawn.detect_terminals()
        ]
    }


class LaunchRequest(BaseModel):
    profile_id: str
    remote_dir: str = ""
    terminal: str = ""
    include_password: bool = True


@router.post("/terminals/launch")
def launch_terminal(request: LaunchRequest, state=Depends(require_unlocked)) -> dict:
    """Start an external terminal on this machine, connected to a profile."""
    profile = state.store().get(request.profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    found = spawn.detect_terminals()
    if not found:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no terminal program was found on this machine",
        )
    chosen = next((t for t in found if t.name == request.terminal), found[0])
    target = spawn.ShellTarget(
        host=profile.host,
        port=profile.effective_port,
        username=profile.username,
        password=profile.password,
        key_path=profile.private_key_path,
        remote_dir=request.remote_dir,
    )
    try:
        spawn.launch(chosen, target, include_password=request.include_password)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"started": chosen.name}
