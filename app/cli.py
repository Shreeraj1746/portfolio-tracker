from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.helpers import get_default_portfolio, hash_password
from app.models import User


def create_user(username: str, password: str) -> None:
    init_db()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.username == username))
        if existing:
            raise SystemExit(f"User '{username}' already exists")

        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        get_default_portfolio(db)
    print(f"Created user '{username}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Tracker CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user_cmd = subparsers.add_parser(
        "create-user", help="Create an initial login user"
    )
    create_user_cmd.add_argument("--username", required=True)
    create_user_cmd.add_argument("--password")

    args = parser.parse_args()

    if args.command == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if len(password) < 8:
            raise SystemExit("Password must be at least 8 characters")
        create_user(args.username, password)


if __name__ == "__main__":
    main()
