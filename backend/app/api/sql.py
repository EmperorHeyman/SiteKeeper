"""Native SQL console endpoints: MySQL and Microsoft SQL Server."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import require_unlocked, verify_token
from app.services import mysql_service
from mysql_runner.db import engines
from mysql_runner.db.driver import MySQLUnavailable, describe_error, driver_available
from mysql_runner.db.mssql_driver import MSSQLUnavailable
from mysql_runner.db.sqlsplit import is_complete

router = APIRouter(prefix="/sql", tags=["sql"], dependencies=[Depends(verify_token)])


class OpenRequest(BaseModel):
    profile_id: str


class RunRequest(BaseModel):
    session_id: str
    sql: str


class CompleteRequest(BaseModel):
    text: str
    #: Which dialect the text is in: "mysql" (the default) or "mssql".
    engine: str = engines.MYSQL


@router.get("/capabilities")
def capabilities() -> dict:
    """Which servers this build can actually open a console on."""
    return {
        # The original key, kept as it was: it means MySQL.
        "driver_available": driver_available(),
        "engines": {
            key: {
                "name": engine.name,
                "available": engine.available(),
                "prompt": engine.prompt,
                "problem": engine.missing_message(),
            }
            for key, engine in (
                (engines.MYSQL, engines.engine(engines.MYSQL)),
                (engines.MSSQL, engines.engine(engines.MSSQL)),
            )
        },
    }


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": mysql_service.manager.list()}


@router.post("/open")
def open_session(request: OpenRequest, state=Depends(require_unlocked)) -> dict:
    profile = state.store().get(request.profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such server")
    if not profile.kind.is_sql:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this connection is not a SQL console profile",
        )
    try:
        info = mysql_service.manager.open(profile)
    except (MySQLUnavailable, MSSQLUnavailable) as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc
    except Exception as exc:
        # A refused connection is the user's problem to see, not a server error.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=describe_error(exc),
        ) from exc
    return info


@router.post("/run")
def run(request: RunRequest) -> dict:
    try:
        session = mysql_service.manager.get(request.session_id)
    except mysql_service.SessionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such session"
        ) from exc
    results = session.run(request.sql)
    return {"session_id": session.id, "database": session.database, "results": results}


@router.post("/complete")
def complete(request: CompleteRequest) -> dict:
    """Whether buffered console input looks terminated (drives the -> prompt)."""
    return {
        "complete": is_complete(
            request.text, tsql=engines.engine(request.engine).tsql
        )
    }


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def close_session(session_id: str) -> None:
    try:
        mysql_service.manager.close(session_id)
    except mysql_service.SessionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such session"
        ) from exc
