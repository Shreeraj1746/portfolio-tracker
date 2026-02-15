from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    """Hash a plain password using PBKDF2."""
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify a plain password against the stored hash."""
    return check_password_hash(password_hash, password)
