# lineage: REQ-AMBIGUOUS / implement.auth_migration (see runs/ for the decision trail)
"""Migration 0003 adds api_keys and links.owner_key_id, and reverses cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from service.db import ALEMBIC_INI


def test_migration_0003_upgrade_and_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "v3.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "0003")
    engine = create_engine(f"sqlite:///{db}")
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("api_keys")} == {
        "id",
        "name",
        "prefix",
        "key_hash",
        "created_at",
        "revoked_at",
    }
    assert "owner_key_id" in {c["name"] for c in insp.get_columns("links")}
    assert any(fk["referred_table"] == "api_keys" for fk in insp.get_foreign_keys("links"))
    command.downgrade(cfg, "0002")
    insp = inspect(engine)
    assert "api_keys" not in insp.get_table_names()
    assert "owner_key_id" not in {c["name"] for c in insp.get_columns("links")}
    engine.dispose()
