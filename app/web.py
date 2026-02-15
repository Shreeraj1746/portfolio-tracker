from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def ensure_csrf_token(request: Request) -> str:
    """Ensure and return a per-session CSRF token."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def form_with_csrf(request: Request) -> Any:
    """Parse request form and validate CSRF token for state-changing requests."""
    form = await request.form()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = request.session.get("csrf_token")
        received = form.get("csrf_token")
        if not expected or received != expected:
            raise HTTPException(status_code=400, detail="Invalid CSRF token")
    return form


def flash(request: Request, message: str, category: str = "info") -> None:
    """Store one flash message in the user's session."""
    flashes = list(request.session.get("_flashes", []))
    flashes.append({"message": message, "category": category})
    request.session["_flashes"] = flashes


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Pop all flash messages from session for one-time display."""
    flashes = list(request.session.get("_flashes", []))
    request.session["_flashes"] = []
    return flashes


def get_user_from_session(request: Request, db: Session) -> User | None:
    """Load the authenticated user based on session state."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.scalar(select(User).where(User.id == int(user_id)))


def redirect_to_login() -> RedirectResponse:
    """Return a redirect response to the login page."""
    return RedirectResponse(url="/login", status_code=303)


def render_template(
    request: Request, template_name: str, context: dict[str, Any]
) -> Any:
    """Render a template with common context variables."""
    templates = request.app.state.templates
    context = dict(context)
    context.setdefault("request", request)
    context.setdefault("csrf_token", ensure_csrf_token(request))
    context.setdefault("flashes", pop_flashes(request))
    return templates.TemplateResponse(template_name, context)
