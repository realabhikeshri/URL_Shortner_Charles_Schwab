# lineage: REQ-BF-002 / implement.migration_0002 (see runs/ for the decision trail)
"""Migration 0002 adds links.expires_at and clicks, and reverses cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from service.db import ALEMBIC_INI


def test_migration_0002_upgrade_and_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "0002")
    engine = create_engine(f"sqlite:///{db}")
    insp = inspect(engine)
    assert "expires_at" in {c["name"] for c in insp.get_columns("links")}
    assert {c["name"] for c in insp.get_columns("clicks")} >= {
        "link_id",
        "occurred_at",
        "day",
        "visitor_hash",
        "referrer_host",
        "ua_family",
    }
    assert "ip" not in {c["name"] for c in insp.get_columns("clicks")}
    command.downgrade(cfg, "0001")
    insp = inspect(engine)
    assert "clicks" not in insp.get_table_names()
    assert "expires_at" not in {c["name"] for c in insp.get_columns("links")}
    engine.dispose()
