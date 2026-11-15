"""A small accounts API. It knows nothing about the migration beyond calling the repository."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from . import config
from .migration.flags import RuntimeSettings, StateStore
from .migration.repository import AccountRepository
from .stores import MongoStore, MySQLStore

Plan = Literal["free", "pro", "enterprise"]


class AccountIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    plan: Plan = "free"
    settings: dict = Field(default_factory=dict)


class AccountPatch(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=255)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    plan: Plan | None = None
    settings: dict | None = None


def create_app(repo: AccountRepository | None = None) -> FastAPI:
    mysql = None
    if repo is None:
        mongo, mysql = MongoStore(), MySQLStore()
        fast_shadows = {
            "mongo": MongoStore(timeout=config.SHADOW_TIMEOUT_SECONDS),
            "mysql": MySQLStore(timeout=config.SHADOW_TIMEOUT_SECONDS),
        }
        repo = AccountRepository(mongo, mysql, RuntimeSettings(StateStore(mysql.engine)), fast_shadows)

    @asynccontextmanager
    async def lifespan(_app):
        if mysql is not None:
            mysql.create_schema()
        yield

    app = FastAPI(title="accounts (migration demo)", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "flags": asdict(repo.settings.flags())}

    @app.get("/metrics")
    def metrics():
        return {"flags": asdict(repo.settings.flags()), "fault_injection": repo.fault_injection, **repo.counters()}

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountIn):
        return repo.create(body.model_dump()).to_api()

    @app.get("/accounts/{id}")
    def get_account(id: str):
        account = repo.get(id)
        if account is None:
            raise HTTPException(404, "account not found")
        return account.to_api()

    @app.patch("/accounts/{id}")
    def update_account(id: str, body: AccountPatch):
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(400, "no changes")
        account = repo.update(id, changes)
        if account is None:
            raise HTTPException(404, "account not found")
        return account.to_api()

    @app.delete("/accounts/{id}", status_code=204)
    def delete_account(id: str):
        if not repo.delete(id):
            raise HTTPException(404, "account not found")
        return Response(status_code=204)

    return app


app = create_app()
