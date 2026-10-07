# lineage: REQ-GF-001 / implement.foundation, REQ-AMBIGUOUS / implement.api_key_auth
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from service.app import create_app
from service.auth import create_api_key
from service.config import Settings
from service.db import run_migrations


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real Alembic migrations once; each test gets a copy of the result."""
    path = tmp_path_factory.mktemp("db") / "template.db"
    run_migrations(f"sqlite+aiosqlite:///{path}")
    return path


@pytest.fixture
def settings(tmp_path: Path, migrated_template: Path) -> Settings:
    db = tmp_path / "test.db"
    shutil.copyfile(migrated_template, db)
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db}",
        db_auto_migrate=False,
        base_url="http://sho.rt/",
        environment="test",
        log_json=True,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


async def issue_key(app: FastAPI, name: str = "test") -> str:
    """Create an API key in the app's database (write endpoints require one)."""
    async with app.state.db.session() as session:
        return await create_api_key(session, name)


def authed(app: FastAPI, key: str, base_url: str = "http://testserver") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url=base_url, headers={"X-API-Key": key}
    )


@pytest.fixture
async def api_key(app: FastAPI) -> str:
    return await issue_key(app)


@pytest.fixture
async def client(app: FastAPI, api_key: str) -> AsyncIterator[AsyncClient]:
    """Authenticated client (the common case for write endpoints)."""
    async with authed(app, api_key) as c:
        yield c


@pytest.fixture
async def anon_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
