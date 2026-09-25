"""Authentication.

* Apache Hop workflows call the API with the service token (HOP_API_TOKEN) as the steward user; the admin
  workflows use a separate HOP_ADMIN_TOKEN.
* AUTH_MODE=otp (MVP2): users log in with email + one-time code and get a short-lived JWT.
* AUTH_MODE=dev: the X-User-Email header identifies the user (local development only).
"""
import hashlib
import hmac
import logging
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import OtpCode, SessionLocal, User

log = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _hash(code: str) -> str:
    return hashlib.sha256((get_settings().jwt_secret + code).encode()).hexdigest()


def ensure_user(db: Session, email: str, role: str = "user", department: str = "") -> User:
    u = db.scalar(select(User).where(User.email == email.lower()))
    if not u:
        u = User(email=email.lower(), role=role, department=department)
        db.add(u)
        db.commit()
    return u


def request_otp(db: Session, email: str) -> None:
    s = get_settings()
    user = db.scalar(select(User).where(User.email == email.lower()))
    if not user:
        return   # do not reveal whether an account exists
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(OtpCode(email=user.email, code_hash=_hash(code),
                   expires_at=datetime.now(timezone.utc) + timedelta(seconds=s.otp_ttl_seconds)))
    db.commit()
    if s.smtp_host:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = "Your DataFusion sign-in code", s.smtp_from, user.email
        msg.set_content(f"Your DataFusion code is {code}. It expires in {s.otp_ttl_seconds // 60} minutes.")
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        log.warning("SMTP not configured; OTP for %s is %s (development only)", user.email, code)


def verify_otp(db: Session, email: str, code: str) -> str:
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(OtpCode).where(OtpCode.email == email.lower(), OtpCode.used.is_(False))).all()
    for row in rows:
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if exp > now and hmac.compare_digest(row.code_hash, _hash(code)):
            row.used = True
            db.commit()
            return jwt.encode({"sub": email.lower(), "exp": now + timedelta(hours=8)}, get_settings().jwt_secret,
                              algorithm="HS256")
    raise HTTPException(401, "Invalid or expired code.")


def current_user(authorization: str = Header(default=""), x_user_email: str = Header(default=""),
                 db: Session = Depends(get_db)) -> User:
    s = get_settings()
    token = authorization.removeprefix("Bearer ").strip()
    if token and hmac.compare_digest(token, s.hop_api_token):
        return ensure_user(db, s.hop_api_user, role="steward", department="data-office")
    if token and s.hop_admin_token and hmac.compare_digest(token, s.hop_admin_token):
        return ensure_user(db, s.hop_admin_user, role="admin", department="data-office")
    if token:
        try:
            email = jwt.decode(token, s.jwt_secret, algorithms=["HS256"])["sub"]
        except jwt.PyJWTError as exc:
            raise HTTPException(401, "Invalid or expired token.") from exc
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            raise HTTPException(401, "Unknown user.")
        return user
    if s.auth_mode == "dev" and x_user_email:
        return ensure_user(db, x_user_email, role="admin")
    raise HTTPException(401, "Sign in required.")


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"Requires role: {', '.join(roles)}.")
        return user
    return dep
