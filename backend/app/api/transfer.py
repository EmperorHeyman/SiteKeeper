"""FTP/FTPS/SFTP endpoints for the dual-pane file manager.

Listings and small mutations answer inline; transfers are queued on the
session's worker thread and report progress over the WebSocket.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import require_unlocked, verify_token
from app.services import transfer_service
from mysql_runner.transfer.base import TransferError, Unsupported
from mysql_runner.transfer.sftp_client import driver_available as sftp_available

router = APIRouter(
    prefix="/transfer", tags=["transfer"], dependencies=[Depends(verify_token)]
)


class OpenRequest(BaseModel):
    profile_id: str


class ListRequest(BaseModel):
    session_id: str
    path: str = ""


class MkdirRequest(BaseModel):
    session_id: str
    path: str


class DeleteRequest(BaseModel):
    session_id: str
    path: str
    is_dir: bool


class RenameRequest(BaseModel):
    session_id: str
    source: str
    target: str


class QueueRequest(BaseModel):
    session_id: str
    #: [path, is_dir] pairs from the selected pane.
    items: list[tuple[str, bool]]
    #: Directory on the receiving side.
    target: str
    #: Apply .deployignore / .gitignore; None means "whatever the setting says".
    use_ignore: bool | None = None


class ItemRequest(BaseModel):
    session_id: str
    item_id: str


class ReorderRequest(BaseModel):
    session_id: str
    item_ids: list[str]


class OptionsRequest(BaseModel):
    session_id: str
    workers: int | None = None
    atomic: bool | None = None
    keep_backups: bool | None = None
    verify: bool | None = None
    use_ignore_rules: bool | None = None


class ChmodRequest(BaseModel):
    session_id: str
    path: str
    mode: int
    recursive: bool = False
    scope: str = "all"


class SymlinkRequest(BaseModel):
    session_id: str
    target: str
    link_path: str


class CompareRequest(BaseModel):
    session_id: str
    local_dir: str
    remote_dir: str
    with_hashes: bool = True
    use_ignore: bool | None = None


class FolderStatsRequest(BaseModel):
    session_id: str
    parent: str
    names: list[str]


class PathRequest(BaseModel):
    session_id: str
    path: str


class GrepRequest(BaseModel):
    session_id: str
    root: str
    pattern: str
    fixed: bool = True
    ignore_case: bool = False
    include: str = ""


class ExecRequest(BaseModel):
    session_id: str
    command: str
    cwd: str = ""


class TailRequest(BaseModel):
    session_id: str
    path: str
    lines: int = 200


class ArchiveRequest(BaseModel):
    session_id: str
    directory: str
    names: list[str]
    archive: str
    kind: str = "tar.gz"


class ExtractRequest(BaseModel):
    session_id: str
    archive: str
    destination: str


class UndoRequest(BaseModel):
    session_id: str
    entry_id: str


def _session(session_id: str) -> transfer_service.TransferSession:
    try:
        return transfer_service.manager.get(session_id)
    except transfer_service.SessionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such session"
        ) from exc


@contextmanager
def _reporting():
    """Turn a backend failure into an HTTP status the UI can show.

    ``Unsupported`` means the protocol cannot do it at all, which is a bad
    request rather than a server fault; anything else the server refused is a
    bad gateway.
    """
    try:
        yield
    except Unsupported as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("/capabilities")
def capabilities() -> dict:
    return {"sftp_available": sftp_available(), "ftp_available": True}


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": transfer_service.manager.list()}


@router.post("/open")
def open_session(request: OpenRequest, state=Depends(require_unlocked)) -> dict:
    profile = state.store().get(request.profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    if not profile.kind.is_transfer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this connection is not a file-transfer profile",
        )
    try:
        return transfer_service.manager.open(profile)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or exc.__class__.__name__,
        ) from exc


@router.post("/home")
def home(request: ListRequest) -> dict:
    session = _session(request.session_id)
    try:
        path = session.home()
        return session.listdir(path)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post("/list")
def list_dir(request: ListRequest) -> dict:
    session = _session(request.session_id)
    try:
        return session.listdir(request.path)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post("/mkdir")
def mkdir(request: MkdirRequest) -> dict:
    session = _session(request.session_id)
    try:
        session.mkdir(request.path)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.post("/delete")
def delete(request: DeleteRequest) -> dict:
    session = _session(request.session_id)
    try:
        session.delete(request.path, request.is_dir)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.post("/rename")
def rename(request: RenameRequest) -> dict:
    session = _session(request.session_id)
    try:
        session.rename(request.source, request.target)
    except TransferError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.post("/download")
def download(request: QueueRequest) -> dict:
    session = _session(request.session_id)
    if not os.path.isdir(request.target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{request.target} is not a directory",
        )
    with _reporting():
        return session.enqueue(
            upload=False,
            items=list(request.items),
            target=request.target,
            use_ignore=request.use_ignore,
        )


@router.post("/upload")
def upload(request: QueueRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.enqueue(
            upload=True,
            items=list(request.items),
            target=request.target,
            use_ignore=request.use_ignore,
        )


@router.post("/cancel")
def cancel(request: ListRequest) -> dict:
    _session(request.session_id).cancel()
    return {"ok": True}


# ----- the queue ----------------------------------------------------------
@router.post("/queue")
def queue(request: ListRequest) -> dict:
    """Every item in the queue, with the totals for the header."""
    return _session(request.session_id).queue()


@router.post("/queue/pause")
def pause(request: ListRequest) -> dict:
    _session(request.session_id).pause()
    return {"ok": True}


@router.post("/queue/resume")
def resume(request: ListRequest) -> dict:
    _session(request.session_id).resume()
    return {"ok": True}


@router.post("/queue/cancel-item")
def cancel_item(request: ItemRequest) -> dict:
    return {"ok": _session(request.session_id).cancel_item(request.item_id)}


@router.post("/queue/prioritize")
def prioritize(request: ItemRequest) -> dict:
    return {"ok": _session(request.session_id).prioritize(request.item_id)}


@router.post("/queue/reorder")
def reorder(request: ReorderRequest) -> dict:
    _session(request.session_id).reorder(list(request.item_ids))
    return {"ok": True}


@router.post("/queue/clear-finished")
def clear_finished(request: ListRequest) -> dict:
    return {"removed": _session(request.session_id).clear_finished()}


@router.post("/options")
def set_options(request: OptionsRequest) -> dict:
    session = _session(request.session_id)
    return {
        "options": session.set_options(
            workers=request.workers,
            atomic=request.atomic,
            keep_backups=request.keep_backups,
            verify=request.verify,
            use_ignore_rules=request.use_ignore_rules,
        )
    }


# ----- permissions and links ---------------------------------------------
@router.post("/chmod")
def chmod(request: ChmodRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        session.chmod(
            request.path,
            request.mode,
            recursive=request.recursive,
            scope=request.scope,
        )
    return {"ok": True}


@router.post("/symlink")
def symlink(request: SymlinkRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        session.symlink(request.target, request.link_path)
    return {"ok": True}


@router.post("/link-target")
def link_target(request: PathRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return {"target": session.link_target(request.path)}


@router.get("/permission-presets")
def permission_presets() -> dict:
    """The chmod presets, so the web UI offers the same list as the desktop."""
    from mysql_runner.transfer import permissions as perm

    return {
        "presets": [
            {
                "label": preset.label,
                "mode": preset.mode,
                "octal": perm.to_octal(preset.mode),
                "note": preset.note,
                "scope": preset.scope,
                "risky": preset.risky,
            }
            for preset in perm.PRESETS
        ]
    }


# ----- comparison and folder statistics ----------------------------------
@router.post("/compare")
def compare(request: CompareRequest) -> dict:
    session = _session(request.session_id)
    if not os.path.isdir(request.local_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{request.local_dir} is not a directory",
        )
    with _reporting():
        return session.compare(
            request.local_dir,
            request.remote_dir,
            with_hashes=request.with_hashes,
            use_ignore=request.use_ignore,
        )


@router.post("/folder-stats")
def folder_stats(request: FolderStatsRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.folder_stats(request.parent, list(request.names))


@router.post("/digest")
def digest(request: PathRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.digest(request.path)


# ----- server-side tools -------------------------------------------------
@router.post("/grep")
def grep(request: GrepRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.grep(
            request.root,
            request.pattern,
            fixed=request.fixed,
            ignore_case=request.ignore_case,
            include=request.include,
        )


@router.post("/disk-usage")
def disk_usage(request: PathRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.disk_usage(request.path)


@router.post("/exec")
def run_command(request: ExecRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.run_command(request.command, request.cwd)


@router.post("/logs")
def logs(request: PathRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return {"logs": session.logs(request.path)}


@router.post("/tail")
def tail(request: TailRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return {"text": session.tail(request.path, lines=request.lines)}


@router.post("/archive")
def archive(request: ArchiveRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.archive(
            request.directory, list(request.names), request.archive, request.kind
        )


@router.post("/extract")
def extract(request: ExtractRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return session.extract(request.archive, request.destination)


# ----- replace history ---------------------------------------------------
@router.post("/history")
def history(request: ListRequest) -> dict:
    return {"entries": _session(request.session_id).history()}


@router.post("/undo")
def undo(request: UndoRequest) -> dict:
    session = _session(request.session_id)
    with _reporting():
        return {"message": session.undo(request.entry_id)}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def close_session(session_id: str) -> None:
    try:
        transfer_service.manager.close(session_id)
    except transfer_service.SessionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such session"
        ) from exc


# ----- the local side of the dual pane -----------------------------------
@router.get("/local")
def local_listing(path: str = "") -> dict:
    """List a local directory, in the same shape as a remote listing."""
    target = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{target} is not a directory",
        )
    entries = []
    try:
        with os.scandir(target) as scan:
            for item in scan:
                try:
                    stat_result = item.stat()
                    size, modified = stat_result.st_size, stat_result.st_mtime
                except OSError:
                    size, modified = 0, None
                entries.append(
                    {
                        "name": item.name,
                        "is_dir": item.is_dir(),
                        "size": size,
                        "modified": modified,
                        "is_link": item.is_symlink(),
                    }
                )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {
        "path": target,
        "at_root": os.path.dirname(target) == target,
        "parent": os.path.dirname(target.rstrip("\\/")) or target,
        "entries": entries,
    }


class LocalMutationRequest(BaseModel):
    path: str
    is_dir: bool = False


@router.post("/local/mkdir")
def local_mkdir(request: LocalMutationRequest) -> dict:
    try:
        os.mkdir(request.path)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.post("/local/delete")
def local_delete(request: LocalMutationRequest) -> dict:
    try:
        if request.is_dir:
            shutil.rmtree(request.path)
        else:
            os.unlink(request.path)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"ok": True}


class LocalRenameRequest(BaseModel):
    source: str
    target: str


@router.post("/local/rename")
def local_rename(request: LocalRenameRequest) -> dict:
    try:
        os.rename(request.source, request.target)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.get("/local/home")
def local_home() -> dict:
    return {"path": os.path.expanduser("~"), "now": datetime.now().isoformat()}
