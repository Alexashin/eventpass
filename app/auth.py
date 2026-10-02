import hashlib
import hmac
import os

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from .models import User, UserRole
from .timeutils import utcnow_naive


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$", 5)
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == username, User.is_active.is_(True)).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow_naive()
    db.commit()
    return user


def current_user(request: Request, db: Session) -> User | None:
    raw_user_id = request.session.get("user_id")
    if raw_user_id is None:
        return None
    try:
        user = db.get(User, int(raw_user_id))
    except (TypeError, ValueError):
        request.session.clear()
        return None
    if not user or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(request: Request, db: Session, roles: set[UserRole] | None = None) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация")
    if roles and user.role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return user
