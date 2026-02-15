from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import Portfolio, User


def hash_password(password: str) -> str:
    """Create a salted password hash."""
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against hash."""
    return check_password_hash(password_hash, password)


def get_or_create_csrf_token(request: Request) -> str:
    """Return CSRF token stored in session."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, token: str | None) -> bool:
    """Validate CSRF token from form payload."""
    return bool(token and request.session.get("csrf_token") and secrets.compare_digest(token, request.session["csrf_token"]))


def parse_form_datetime(value: str) -> datetime:
    """Parse HTML datetime-local value as UTC timestamp."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def set_flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}


def pop_flash(request: Request) -> dict[str, str] | None:
    flash = request.session.get("flash")
    if flash:
        request.session.pop("flash", None)
    return flash


def get_default_portfolio(db: Session) -> Portfolio:
    """Use a single default portfolio for MVP, auto-creating when missing."""
    portfolio = db.scalar(select(Portfolio).order_by(Portfolio.id.asc()))
    if portfolio:
        return portfolio
    portfolio = Portfolio(name="Default")
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


def get_logged_in_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)
