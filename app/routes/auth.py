from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import verify_password
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter()


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    """Render login form."""
    user = get_user_from_session(request, db)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return render_template(request, "login.html", {"page_title": "Login"})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    """Validate credentials and start a session."""
    form = await form_with_csrf(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    user = db.scalar(select(User).where(User.username == username))
    if not user or not verify_password(user.password_hash, password):
        flash(request, "Invalid username or password", "error")
        return RedirectResponse(url="/login", status_code=303)

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    flash(request, "Logged in", "success")
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    """End current user session."""
    form = await form_with_csrf(request)
    _ = form
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
