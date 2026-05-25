import pytest
from unittest.mock import MagicMock, patch

from models import PlexUser, JellyfinUser
from jellyfin_client import JellyFinServer
from user_manager import discover_plex_users, resolve_jellyfin_user


class TestDiscoverPlexUsers:
    def test_includes_main_account(self):
        plex = MagicMock()
        plex.myPlexAccount.return_value.title = "admin"
        plex.myPlexAccount.return_value.users.return_value = []

        users = discover_plex_users(plex, base_token="main_token")

        assert users[0] == PlexUser(name="admin", token="main_token", is_managed=False)

    def test_includes_managed_users(self):
        managed = MagicMock()
        managed.title = "kid"
        managed.get_token.return_value = "kid_token"

        plex = MagicMock()
        plex.myPlexAccount.return_value.title = "admin"
        plex.myPlexAccount.return_value.users.return_value = [managed]
        plex.machineIdentifier = "server123"

        users = discover_plex_users(plex, base_token="main_token")

        assert PlexUser(name="kid", token="kid_token", is_managed=True) in users

    def test_skips_user_when_token_fetch_fails(self):
        managed = MagicMock()
        managed.title = "broken"
        managed.get_token.side_effect = Exception("auth failed")

        plex = MagicMock()
        plex.myPlexAccount.return_value.title = "admin"
        plex.myPlexAccount.return_value.users.return_value = [managed]
        plex.machineIdentifier = "server123"

        users = discover_plex_users(plex, base_token="main_token")

        assert len(users) == 1
        assert users[0].name == "admin"

    def test_skips_user_when_token_is_none(self):
        managed = MagicMock()
        managed.title = "nonetoken"
        managed.get_token.return_value = None

        plex = MagicMock()
        plex.myPlexAccount.return_value.title = "admin"
        plex.myPlexAccount.return_value.users.return_value = [managed]
        plex.machineIdentifier = "server123"

        users = discover_plex_users(plex, base_token="main_token")
        names = [u.name for u in users]
        assert "nonetoken" not in names


@pytest.fixture
def jf():
    client = MagicMock(spec=JellyFinServer)
    client.get_users.return_value = [
        JellyfinUser(id="1", name="Alice"),
        JellyfinUser(id="2", name="Bob"),
    ]
    return client


class TestResolveJellyfinUser:
    def test_exact_match(self, jf):
        user = resolve_jellyfin_user(jf, "Alice", auto_create=False, dry_run=False)
        assert user == JellyfinUser(id="1", name="Alice")

    def test_case_insensitive_match(self, jf):
        user = resolve_jellyfin_user(jf, "alice", auto_create=False, dry_run=False)
        assert user == JellyfinUser(id="1", name="Alice")

    def test_returns_none_when_no_match_and_no_create(self, jf):
        user = resolve_jellyfin_user(jf, "Carol", auto_create=False, dry_run=False)
        assert user is None

    def test_creates_user_when_no_match_and_auto_create(self, jf):
        jf.create_user.return_value = JellyfinUser(id="3", name="Carol")
        user = resolve_jellyfin_user(jf, "Carol", auto_create=True, dry_run=False)
        assert user == JellyfinUser(id="3", name="Carol")
        jf.create_user.assert_called_once()
        _, created_password = jf.create_user.call_args.args
        assert len(created_password) == 16

    def test_dry_run_does_not_create_user(self, jf):
        user = resolve_jellyfin_user(jf, "Carol", auto_create=True, dry_run=True)
        assert user is None
        jf.create_user.assert_not_called()

    def test_exact_match_preferred_over_case_insensitive(self, jf):
        jf.get_users.return_value = [
            JellyfinUser(id="1", name="alice"),
            JellyfinUser(id="2", name="Alice"),
        ]
        user = resolve_jellyfin_user(jf, "Alice", auto_create=False, dry_run=False)
        assert user.id == "2"
