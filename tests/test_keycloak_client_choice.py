"""Which realm client the Keycloak login signs in through."""

from scriptit_cli import auth

_BASE = {"url": "http://kc", "realm": "scriptit", "client_id": "scriptit-web"}


def _client_used_for(monkeypatch, keycloak_config: dict) -> str:
    seen: dict = {}

    def fake_device_login(url, realm, client_id, timeout=300):
        seen["client_id"] = client_id
        return {
            "mode": "keycloak",
            "token_url": f"{url}/realms/{realm}/protocol/openid-connect/token",
            "client_id": client_id,
            "id_token": "id",
            "refresh_token": "r",
            "id_token_expires_at": 0,
        }

    monkeypatch.setattr(auth, "keycloak_device_login", fake_device_login)
    monkeypatch.setattr(auth, "save_credentials", lambda creds, profile=None: None)
    monkeypatch.setattr(auth, "_verify_stored_login", lambda api_url, creds, profile: None)
    auth.AuthCommands()._login_keycloak(
        "http://api", {"mode": "keycloak", "keycloak": keycloak_config}, 30
    )
    return seen["client_id"]


def test_prefers_the_realm_cli_client_the_backend_names(monkeypatch) -> None:
    """A session issued to the CLI's own client is what the backend treats as
    an outside agent, so the account can govern it; signing in through the
    web client would leave the session indistinguishable from a browser's."""
    assert (
        _client_used_for(monkeypatch, {**_BASE, "cli_client_id": "scriptit-cli"}) == "scriptit-cli"
    )


def test_falls_back_to_the_web_client_for_a_backend_that_predates_the_field(monkeypatch) -> None:
    assert _client_used_for(monkeypatch, _BASE) == "scriptit-web"
    assert _client_used_for(monkeypatch, {**_BASE, "cli_client_id": None}) == "scriptit-web"
