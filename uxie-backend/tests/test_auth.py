"""Tests for OTP send/verify and JWT auth."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from db import OTP, User
from tests.conftest import _TestSessionLocal, create_user_and_token


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_send_otp_creates_db_record(client: AsyncClient):
    resp = await client.post("/auth/send-otp", json={"email": "alice@example.com"})
    # Resend is not configured in tests — expect 500 from email send, but OTP row created
    # We patch _send_email so it doesn't actually call Resend.
    # Since RESEND_API_KEY is set to a dummy value, _send_email will raise → 500.
    # We verify OTP was created before the email step.
    # Better: mock _send_email in the test.
    pass  # covered by test_full_otp_flow below


@pytest.mark.asyncio
async def test_full_otp_flow(client: AsyncClient, monkeypatch):
    """Full auth flow: send OTP → verify → get token → access protected route."""
    # Mock _send_email to avoid calling Resend
    import auth
    monkeypatch.setattr(auth, "_send_email", _mock_send_email)

    email = "bob@example.com"
    resp = await client.post("/auth/send-otp", json={"email": email})
    assert resp.status_code == 200

    # Read OTP from DB
    async with _TestSessionLocal() as s:
        result = await s.execute(select(OTP).where(OTP.email == email, OTP.used == False))
        otp = result.scalars().first()
    assert otp is not None
    code = otp.code

    # Verify OTP
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": code})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["tier"] == "free"
    assert data["free_days_remaining"] == 30

    # Access protected route
    token = data["access_token"]
    resp = await client.get("/user/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == email


@pytest.mark.asyncio
async def test_otp_wrong_code(client: AsyncClient, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "_send_email", _mock_send_email)

    email = "charlie@example.com"
    await client.post("/auth/send-otp", json={"email": email})
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": "000000"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_otp_reuse_fails(client: AsyncClient, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "_send_email", _mock_send_email)

    email = "dave@example.com"
    await client.post("/auth/send-otp", json={"email": email})

    async with _TestSessionLocal() as s:
        result = await s.execute(select(OTP).where(OTP.email == email, OTP.used == False))
        code = result.scalars().first().code

    # First verify succeeds
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": code})
    assert resp.status_code == 200

    # Second verify with same code fails (OTP is now used)
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": code})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_protected_route_without_token(client: AsyncClient):
    resp = await client.get("/user/status")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_protected_route_bad_token(client: AsyncClient):
    resp = await client.get("/user/status", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


async def _mock_send_email(to: str, code: str):
    pass  # no-op: don't call Resend in tests


@pytest.mark.asyncio
async def test_otp_locks_after_max_wrong_attempts(client: AsyncClient, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "_send_email", _mock_send_email)

    email = "lockout@example.com"
    await client.post("/auth/send-otp", json={"email": email})
    async with _TestSessionLocal() as s:
        real = (await s.execute(select(OTP.code).where(OTP.email == email, OTP.used == False))).scalar_one()
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(auth.OTP_MAX_ATTEMPTS - 1):
        resp = await client.post("/auth/verify-otp", json={"email": email, "code": wrong})
        assert resp.json()["detail"] == "Invalid or expired OTP"
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": wrong})
    assert resp.json()["detail"] == "Too many attempts — request a new code"

    # The correct code no longer works once locked.
    resp = await client.post("/auth/verify-otp", json={"email": email, "code": real})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stt_session_never_returns_master_key(client: AsyncClient, monkeypatch):
    import proxy
    token = await create_user_and_token(client, "stt-fail@example.com")
    calls = []

    async def _fail(user_id):
        calls.append(user_id)
        return None, None

    monkeypatch.setattr(proxy, "_mint_deepgram_key", _fail)
    resp = await client.post("/stt/session", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503
    assert len(calls) == 2  # one retry
