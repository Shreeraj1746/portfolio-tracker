from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import User
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Tracker management commands")
    sub = parser.add_subparsers(dest="command", required=True)

    create_user_parser = sub.add_parser("create-user", help="Create initial login user")
    create_user_parser.add_argument("--username", required=True, help="Username for the new user")

    args = parser.parse_args()

    init_db()

    if args.command == "create-user":
        user_id = create_user(args.username)
        print(f"Created user id={user_id} username={args.username}")


if __name__ == "__main__":
    main()
