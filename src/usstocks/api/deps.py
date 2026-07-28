"""Shared API state.

The API opens its own SQLite connections. It reads bars and writes only the
small ``symbols`` table, which is the documented exception to the spec's
single-writer rule (docs/spec-review.md A-3).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..adapters.base import MarketDataAdapter
from ..config import Settings
from ..db.live_store import LiveStore
from ..db.repository import Repository
from .auth import AccessVerifier


@dataclass
class AppState:
    settings: Settings
    repository: Repository
    live_store: LiveStore
    verifier: AccessVerifier | None
    adapter: MarketDataAdapter | None = None

    def close(self) -> None:
        self.repository.close()
        self.live_store.close()


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_settings_dep(request: Request) -> Settings:
    return get_state(request).settings


def get_repository(request: Request) -> Repository:
    return get_state(request).repository


def get_live_store(request: Request) -> LiveStore:
    return get_state(request).live_store
