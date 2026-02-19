from __future__ import annotations

import argparse
import getpass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Asset, User
from app.security import hash_password
from app.services.portfolio import ensure_default_portfolio


def create_user(username: str) -> int:
    """Create one user with a securely hashed password."""
    password = getpass.getpass(prompt="Password: ")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.username == username))
        if existing:
            raise ValueError(f"User '{username}' already exists")

        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        ensure_default_portfolio(db)
        db.commit()
        return user.id


def purge_archived_assets(older_than_days: int | None = None) -> int:
    """Permanently delete archived assets and their dependent rows."""
    with SessionLocal() as db:
        query = select(Asset).where(Asset.is_archived.is_(True))
        if older_than_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            query = query.where(Asset.created_at <= cutoff)

        archived_assets = list(db.scalars(query))
        deleted_count = len(archived_assets)
        for asset in archived_assets:
            db.delete(asset)
        db.commit()
        return deleted_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Portfolio Tracker management commands"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_user_parser = sub.add_parser("create-user", help="Create initial login user")
    create_user_parser.add_argument(
        "--username", required=True, help="Username for the new user"
    )
    purge_parser = sub.add_parser(
        "purge-archived-assets",
        help="Permanently delete archived assets",
    )
    purge_parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="Only delete archived assets created at least N days ago",
    )

    args = parser.parse_args()

    init_db()

    if args.command == "create-user":
        user_id = create_user(args.username)
        print(f"Created user id={user_id} username={args.username}")
    elif args.command == "purge-archived-assets":
        if args.older_than_days is not None and args.older_than_days < 0:
            raise ValueError("--older-than-days must be zero or greater")
        deleted = purge_archived_assets(args.older_than_days)
        print(f"Deleted {deleted} archived asset(s)")


if __name__ == "__main__":
    main()
