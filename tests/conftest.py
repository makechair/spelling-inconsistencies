from __future__ import annotations

from pathlib import Path

import pytest

from usstocks.config import Settings
from usstocks.db.live_store import LiveStore
from usstocks.db.migrate import migrate, migrate_live
from usstocks.db.repository import Repository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    migrate(path)
    return path


@pytest.fixture
def repo(db_path: Path) -> Repository:
    repository = Repository(db_path)
    yield repository
    repository.close()


@pytest.fixture
def live_path(tmp_path: Path) -> Path:
    path = tmp_path / "live.db"
    migrate_live(path)
    return path


@pytest.fixture
def live_store(live_path: Path) -> LiveStore:
    store = LiveStore(live_path)
    yield store
    store.close()


@pytest.fixture
def settings(tmp_path: Path, db_path: Path, live_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        live_db_path=live_path,
        auth_mode="disabled",
        api_host="127.0.0.1",
        primary_source="mock",
        web_dir=Path(__file__).resolve().parents[1] / "web",
    )
