# lineage: REQ-AMBIGUOUS / implement.api_key_auth (see runs/ for the decision trail)
"""Operational CLI.

python -m service.cli migrate
python -m service.cli create-key --name ci-pipeline
python -m service.cli list-keys
python -m service.cli revoke-key <prefix>
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from service.auth import create_api_key, revoke_api_key
from service.config import Settings
from service.db import Database, migrate
from service.models import ApiKey


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "migrate":
        await migrate(settings.database_url)
        print("database migrated to head")
        return 0
    db = Database(settings.database_url)
    try:
        async with db.session() as session:
            if args.command == "create-key":
                key = await create_api_key(session, args.name)
                print(key)
                print("store this key now; it cannot be shown again", file=sys.stderr)
                return 0
            if args.command == "revoke-key":
                ok = await revoke_api_key(session, args.prefix)
                print("revoked" if ok else "no active key with that prefix")
                return 0 if ok else 1
            rows = (await session.execute(select(ApiKey).order_by(ApiKey.id))).scalars().all()
            for row in rows:
                state = "revoked" if row.revoked_at else "active"
                print(f"{row.prefix}\t{row.name}\t{state}\t{row.created_at.isoformat()}")
            return 0
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m service.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="upgrade the database schema to head")
    create = sub.add_parser("create-key", help="create an API key (printed once)")
    create.add_argument("--name", required=True)
    revoke = sub.add_parser("revoke-key", help="revoke an API key by prefix")
    revoke.add_argument("prefix")
    sub.add_parser("list-keys", help="list API keys (prefix, name, state)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args, Settings()))


if __name__ == "__main__":
    raise SystemExit(main())
