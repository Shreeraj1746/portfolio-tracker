from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.helpers import get_logged_in_user, get_or_create_csrf_token, pop_flash


templates = Jinja2Templates(directory="app/templates")


def require_user(request: Request, db: Session):
    user = get_logged_in_user(request, db)
    if not user:
        return None
    return user


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def base_context(request: Request, db: Session) -> dict:
    user = get_logged_in_user(request, db)
    return {
        "request": request,
        "current_user": user,
        "csrf_token": get_or_create_csrf_token(request),
        "flash": pop_flash(request),
    }
