from __future__ import annotations

from web_channel import settings


def test_settings_env_fallback_and_db_override(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_CLIENT_ID", "from-env")
    assert settings.get("OIDC_CLIENT_ID") is None
    assert settings.get_or_env("OIDC_CLIENT_ID") == "from-env"
    assert settings.get_or_env("MISSING_SETTING", "fallback") == "fallback"

    settings.put("OIDC_CLIENT_ID", "from-db")
    assert settings.get("OIDC_CLIENT_ID") == "from-db"
    assert settings.get_or_env("OIDC_CLIENT_ID") == "from-db"


def test_settings_callbacks_fire_after_put() -> None:
    calls = {"n": 0}

    def changed() -> None:
        calls["n"] += 1

    settings.register_on_change(changed)
    settings.put("OIDC_ISSUER", "http://kc.test/realms/swarm")
    assert calls["n"] == 1
