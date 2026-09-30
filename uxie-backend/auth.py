"""
Authentication — OTP via Resend + RS256 JWT.

Flow:
  POST /auth/send-otp   → generate 6-digit code, email it via Resend
  POST /auth/verify-otp → validate code → issue 30-day JWT
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import OTP, User, generate_referral_code, get_db
from settings import get_settings

_settings = get_settings()
_bearer = HTTPBearer()


# ── Schemas ───────────────────────────────────────────────────────────────────

class SendOTPRequest(BaseModel):
    email: EmailStr
    referral_code: str | None = None  # code entered during onboarding


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    code: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    tier: str
    referral_code: str
    free_days_remaining: int


# ── OTP helpers ───────────────────────────────────────────────────────────────

OTP_MAX_ATTEMPTS = 5


def _generate_otp() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


async def _send_email(to: str, code: str):
    """Send OTP via Resend. Raises RuntimeError if Resend is not configured."""
    if not _settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    import resend
    resend.api_key = _settings.resend_api_key
    resend.Emails.send({
        "from": _settings.resend_from,
        "to": [to],
        "subject": "Your Uxie login code",
        "html": (
            f"<p>Your Uxie verification code is:</p>"
            f"<h2 style='letter-spacing:0.2em'>{code}</h2>"
            f"<p>This code expires in {_settings.otp_expiry_minutes} minutes.</p>"
        ),
    })


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _issue_jwt(user_id: int) -> str:
    if not _settings.jwt_private_key:
        raise RuntimeError("JWT_PRIVATE_KEY is not set")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=_settings.jwt_expiry_days),
    }
    return jwt.encode(payload, _settings.jwt_private_key, algorithm=_settings.jwt_algorithm)


def _decode_jwt(token: str) -> dict:
    if not _settings.jwt_public_key:
        raise RuntimeError("JWT_PUBLIC_KEY is not set")
    try:
        return jwt.decode(token, _settings.jwt_public_key, algorithms=[_settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")


# ── Route handlers ────────────────────────────────────────────────────────────

async def send_otp(body: SendOTPRequest, db: AsyncSession = Depends(get_db)):
    code = _generate_otp()
    expires = datetime.now(timezone.utc) + timedelta(minutes=_settings.otp_expiry_minutes)

    # Invalidate any previous unused OTPs for this email
    stale = (await db.execute(
        select(OTP).where(OTP.email == body.email, OTP.used == False)
    )).scalars().all()
    for s in stale:
        s.used = True

    otp = OTP(email=body.email, code=code, expires_at=expires)
    db.add(otp)
    await db.commit()

    try:
        await _send_email(body.email, code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {e}")

    return {"detail": "OTP sent"}


async def verify_otp(
    body: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    now = datetime.now(timezone.utc)
    # Match on the email's active code (at most one — send_otp retires older
    # ones), then compare, so wrong guesses count against it.
    otp = (await db.execute(
        select(OTP).where(
            OTP.email == body.email,
            OTP.used == False,
            OTP.expires_at > now,
        ).order_by(OTP.id.desc()).limit(1)
    )).scalar_one_or_none()
    if not otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    if not secrets.compare_digest(otp.code.encode(), body.code.encode()):
        otp.attempts = (otp.attempts or 0) + 1
        locked = otp.attempts >= OTP_MAX_ATTEMPTS
        if locked:
            otp.used = True
        await db.commit()
        if locked:
            raise HTTPException(status_code=400, detail="Too many attempts — request a new code")
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    otp.used = True

    # Get or create user
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            email=body.email,
            referral_code=generate_referral_code(),
            tier="free",
            free_days_remaining=_settings.trial_days,
        )
        db.add(user)
        await db.flush()  # get user.id

    await db.commit()
    await db.refresh(user)

    token = _issue_jwt(user.id)
    return AuthResponse(
        access_token=token,
        user_id=user.id,
        tier=user.tier,
        referral_code=user.referral_code,
        free_days_remaining=user.free_days_remaining,
    )


# ── Auth dependency (used by protected routes) ────────────────────────────────

async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = _decode_jwt(credentials.credentials)
    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def current_user_from_token(token: str, db: AsyncSession) -> User | None:
    """Used by flows that can't pass an Authorization header — notably the
    OAuth `start` endpoint, which is opened in an in-app browser and gets
    the JWT via a query param. Returns None on any decode/lookup failure
    rather than raising — callers want to redirect cleanly, not 401."""
    try:
        payload = _decode_jwt(token)
        user_id = int(payload["sub"])
    except Exception:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
