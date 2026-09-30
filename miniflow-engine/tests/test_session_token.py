"""JWT handoff to Electron safeStorage (config.take_file_token / set_session_token)."""
from __future__ import annotations

import json


def _file(config):
    return json.loads(config._UXIE_AUTH_FILE.read_text())


def test_login_then_adopt_strips_token_from_disk():
    import config
    config.save_jwt("tok1", email="a@b.com")
    assert _file(config)["access_token"] == "tok1"

    assert config.take_file_token() == {"token": "tok1"}
    assert "access_token" not in _file(config)
    assert config.get_jwt() == "tok1"                     # still usable from memory
    assert config.take_file_token() == {"token": None}    # nothing left to adopt


def test_profile_refresh_does_not_rewrite_token_once_secured():
    import config
    config.save_jwt("tok1", email="a@b.com")
    config.take_file_token()
    config.save_jwt("tok1", email="a@b.com", tier="pro")  # /user/status refresh
    data = _file(config)
    assert "access_token" not in data and data["tier"] == "pro"


def test_restart_gets_token_from_electron():
    import config
    config.set_session_token("tok2")
    assert config.get_jwt() == "tok2"


def test_get_uxie_user_never_returns_real_token():
    import config
    config.save_jwt("secret-tok", email="a@b.com")
    user = config.get_uxie_user()
    assert user["access_token"] == "stored" and user["email"] == "a@b.com"


def test_logout_clears_memory_and_file():
    import config
    config.set_session_token("tok3")
    config.clear_jwt()
    assert config.get_jwt() is None
    assert "access_token" not in config.get_uxie_user()
    # A new login after logout goes back to the file until Electron adopts it.
    config.save_jwt("tok4")
    assert _file(config)["access_token"] == "tok4"
