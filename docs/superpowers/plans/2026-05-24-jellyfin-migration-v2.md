# Jellyfin Migration v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor migrate-plex-to-jellyfin into four focused modules with bulk multi-user migration, auto Jellyfin account creation, ratings/favorites migration, all upstream bug fixes, config file support, progress bars, improved Docker setup with Saltbox compose, GHCR publishing, and an overhauled README.

**Architecture:** Extract `models.py` (dataclasses), rewrite `jellyfin_client.py` (typed, paginated, Jellyfin 10.9+ auth), add `user_manager.py` (Plex user discovery + Jellyfin user matching/creation), and refactor `migrate.py` (CLI + `migrate_user()` core function). Config file support merges YAML defaults with CLI overrides via Click's `default_map`. Docker improvements ship alongside a Saltbox-specific compose file and a GHCR publish workflow.

**Tech Stack:** Python 3.12, click, plexapi, requests, loguru, tqdm, PyYAML, pytest, pytest-mock; Docker multi-stage build; GitHub Actions

---

## File Map

| File | Status | Role |
|------|--------|------|
| `models.py` | CREATE | `PlexUser`, `JellyfinUser`, `MigrationStats` dataclasses |
| `jellyfin_client.py` | REWRITE | HTTP client — auth header, pagination, user CRUD, ratings, error handling |
| `user_manager.py` | CREATE | Plex user discovery, Jellyfin user matching + auto-creation |
| `migrate.py` | REWRITE | CLI entrypoint + `migrate_user()` core loop |
| `test_jellyfin_client.py` | CREATE | Unit tests for HTTP client |
| `test_user_manager.py` | CREATE | Unit tests for user discovery/matching |
| `test_migrate.py` | CREATE | Unit tests for `migrate_user()` |
| `test_config.py` | CREATE | Unit tests for config file loading |
| `test_translate.py` | UNCHANGED | Existing path-translation tests |
| `requirements.txt` | MODIFY | Add `tqdm`, `PyYAML` |
| `requirements.dev.txt` | MODIFY | Add `pytest-mock` |
| `dockerfile` | REWRITE | Pinned Python 3.12, multi-stage build |
| `.dockerignore` | CREATE | Exclude .git, docs, venv, __pycache__ |
| `docker-compose.yml` | CREATE | Generic compose with .env + config.yml |
| `docker-compose.saltbox.yml` | CREATE | Saltbox external network variant |
| `.env.example` | CREATE | Template for .env |
| `config.example.yml` | CREATE | Annotated YAML config template |
| `.github/workflows/test.yml` | CREATE | pytest on PRs, Python 3.11 + 3.12 matrix |
| `.github/workflows/publish.yml` | CREATE | Build + push to GHCR on version tags |
| `README.md` | REWRITE | All new flags, config file, Docker Compose, Saltbox section |

---

## Task 1: Add `models.py`

**Files:**
- Create: `models.py`

- [ ] **Step 1: Create `models.py`**

```python
from dataclasses import dataclass, field
from typing import List, NamedTuple


@dataclass
class PlexUser:
    name: str
    token: str
    is_managed: bool


@dataclass
class JellyfinUser:
    id: str
    name: str


@dataclass
class MigrationStats:
    marked: int = 0
    missing: int = 0
    skipped: int = 0
    ratings_set: int = 0
    favorites_set: int = 0
```

- [ ] **Step 2: Verify Python parses it**

```bash
python3 -c "from models import PlexUser, JellyfinUser, MigrationStats; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add models.py
git commit -m "feat: add typed models (PlexUser, JellyfinUser, MigrationStats)"
```

---

## Task 2: Update Requirements

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements.dev.txt`

- [ ] **Step 1: Update `requirements.txt`**

```
plexapi
click
requests
loguru
tqdm
PyYAML
```

- [ ] **Step 2: Update `requirements.dev.txt`**

```
pytest ~= 8.1.0
pytest-mock ~= 3.14.0
```

- [ ] **Step 3: Install updated requirements**

```bash
pip install -r requirements.txt -r requirements.dev.txt
```

Expected: all packages install without error.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt requirements.dev.txt
git commit -m "chore: add tqdm, PyYAML, pytest-mock to requirements"
```

---

## Task 3: Rewrite `jellyfin_client.py` (TDD)

**Files:**
- Create: `test_jellyfin_client.py`
- Rewrite: `jellyfin_client.py`

### Step 3.1 — Write the failing tests

- [ ] **Step 1: Create `test_jellyfin_client.py`**

```python
import pytest
import requests
from unittest.mock import MagicMock
from requests.exceptions import HTTPError

from jellyfin_client import JellyFinServer, JellyfinAPIError
from models import JellyfinUser


@pytest.fixture
def session():
    return MagicMock(spec=requests.Session)


@pytest.fixture
def client(session):
    return JellyFinServer(url="http://jf.test", api_key="secret", session=session)


def ok_response(json_data):
    r = MagicMock()
    r.status_code = 200
    r.ok = True
    r.raise_for_status.return_value = None
    r.json.return_value = json_data
    return r


def error_response(status_code):
    r = MagicMock()
    r.status_code = status_code
    r.ok = False
    r.raise_for_status.side_effect = HTTPError(response=r)
    return r


# --- Auth header ---

class TestHeaders:
    def test_contains_token(self, client):
        h = client._headers()
        assert 'MediaBrowser Token="secret"' in h["Authorization"]

    def test_contains_client_name(self, client):
        h = client._headers()
        assert "migrate-plex-to-jellyfin" in h["Authorization"]


# --- _get error handling ---

class TestGet:
    def test_raises_api_error_on_4xx(self, client, session):
        session.get.return_value = error_response(401)
        with pytest.raises(JellyfinAPIError) as exc:
            client._get("Users")
        assert exc.value.status_code == 401

    def test_raises_api_error_on_bad_json(self, client, session):
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.side_effect = ValueError("bad json")
        session.get.return_value = r
        with pytest.raises(JellyfinAPIError) as exc:
            client._get("Users")
        assert "JSON" in str(exc.value) or "json" in str(exc.value).lower()

    def test_returns_json_on_success(self, client, session):
        session.get.return_value = ok_response({"foo": "bar"})
        assert client._get("anything") == {"foo": "bar"}

    def test_sends_auth_header(self, client, session):
        session.get.return_value = ok_response({})
        client._get("Users")
        call_kwargs = session.get.call_args.kwargs
        assert "Authorization" in call_kwargs["headers"]


# --- iter_items pagination ---

class TestIterItems:
    def test_yields_items_from_single_page(self, client, session):
        items = [{"Id": "a"}, {"Id": "b"}]
        session.get.return_value = ok_response({"Items": items})
        assert list(client.iter_items("u1", page_size=100)) == items

    def test_fetches_second_page_when_first_is_full(self, client, session):
        page1 = [{"Id": str(i)} for i in range(3)]
        page2 = [{"Id": "last"}]
        session.get.side_effect = [
            ok_response({"Items": page1}),
            ok_response({"Items": page2}),
        ]
        result = list(client.iter_items("u1", page_size=3))
        assert len(result) == 4
        assert session.get.call_count == 2

    def test_stops_when_page_smaller_than_limit(self, client, session):
        session.get.return_value = ok_response({"Items": [{"Id": "x"}]})
        list(client.iter_items("u1", page_size=100))
        assert session.get.call_count == 1

    def test_passes_start_index_on_second_call(self, client, session):
        page1 = [{"Id": str(i)} for i in range(2)]
        page2 = [{"Id": "done"}]
        session.get.side_effect = [
            ok_response({"Items": page1}),
            ok_response({"Items": page2}),
        ]
        list(client.iter_items("u1", page_size=2))
        second_call_params = session.get.call_args_list[1].kwargs["params"]
        assert second_call_params["StartIndex"] == 2


# --- get_users ---

class TestGetUsers:
    def test_returns_typed_jellyfin_users(self, client, session):
        session.get.return_value = ok_response([
            {"Id": "1", "Name": "alice"},
            {"Id": "2", "Name": "bob"},
        ])
        users = client.get_users()
        assert users == [JellyfinUser(id="1", name="alice"), JellyfinUser(id="2", name="bob")]


# --- create_user ---

class TestCreateUser:
    def test_calls_new_endpoint_then_password_endpoint(self, client, session):
        session.post.side_effect = [
            ok_response({"Id": "new_id", "Name": "carol"}),
            ok_response({}),
        ]
        user = client.create_user("carol", "s3cr3t")
        assert user == JellyfinUser(id="new_id", name="carol")
        assert session.post.call_count == 2
        first_url = session.post.call_args_list[0].kwargs["url"]
        second_url = session.post.call_args_list[1].kwargs["url"]
        assert "Users/New" in first_url
        assert "new_id" in second_url
        assert "Password" in second_url


# --- mark_watched ---

class TestMarkWatched:
    def test_posts_to_played_items_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.mark_watched("uid", "iid")
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/PlayedItems/iid" in url

    def test_raises_on_failure(self, client, session):
        session.post.return_value = error_response(500)
        with pytest.raises(JellyfinAPIError):
            client.mark_watched("uid", "iid")


# --- set_rating ---

class TestSetRating:
    def test_posts_to_rating_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.set_rating("uid", "iid", 8.0)
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/Items/iid/Rating" in url


# --- mark_favorite ---

class TestMarkFavorite:
    def test_posts_to_favorite_items_endpoint(self, client, session):
        session.post.return_value = ok_response({})
        client.mark_favorite("uid", "iid")
        url = session.post.call_args.kwargs["url"]
        assert "Users/uid/FavoriteItems/iid" in url
```

- [ ] **Step 2: Run tests — verify they all FAIL (module not yet rewritten)**

```bash
pytest test_jellyfin_client.py -v 2>&1 | head -40
```

Expected: import or attribute errors — `JellyfinAPIError` not found, `_headers` not found, etc.

### Step 3.2 — Implement `jellyfin_client.py`

- [ ] **Step 3: Rewrite `jellyfin_client.py`**

```python
from typing import List, Iterator, Optional
from dataclasses import dataclass

import requests

from models import JellyfinUser


class JellyfinAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Jellyfin API error {status_code}: {message}")


@dataclass
class JellyFinServer:
    url: str
    api_key: str
    session: requests.Session

    def _headers(self) -> dict:
        return {
            "Authorization": (
                f'MediaBrowser Token="{self.api_key}", '
                f'Client="migrate-plex-to-jellyfin", Version="2.0"'
            )
        }

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        try:
            r = self.session.get(
                url=f"{self.url}/{endpoint}",
                params=params or {},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            raise JellyfinAPIError(r.status_code, str(e)) from e
        except (ValueError, KeyError) as e:
            raise JellyfinAPIError(
                0,
                f"Invalid JSON response — check Jellyfin version compatibility ({e})",
            ) from e

    def _post(self, endpoint: str, params: Optional[dict] = None, body: Optional[dict] = None) -> requests.Response:
        try:
            r = self.session.post(
                url=f"{self.url}/{endpoint}",
                params=params or {},
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            raise JellyfinAPIError(r.status_code, str(e)) from e

    def get_users(self) -> List[JellyfinUser]:
        users = self._get("Users")
        return [JellyfinUser(id=u["Id"], name=u["Name"]) for u in users]

    def create_user(self, name: str, password: str) -> JellyfinUser:
        r = self._post("Users/New", body={"Name": name})
        data = r.json()
        user = JellyfinUser(id=data["Id"], name=data["Name"])
        self._post(f"Users/{user.id}/Password", body={"NewPw": password})
        return user

    def iter_items(self, user_id: str, page_size: int = 5000) -> Iterator[dict]:
        start = 0
        while True:
            page = self._get(
                f"Users/{user_id}/Items",
                params={
                    "Recursive": True,
                    "Fields": "MediaSources,UserData",
                    "StartIndex": start,
                    "Limit": page_size,
                },
            )
            items = page["Items"]
            yield from items
            if len(items) < page_size:
                break
            start += len(items)

    def mark_watched(self, user_id: str, item_id: str) -> None:
        self._post(f"Users/{user_id}/PlayedItems/{item_id}")

    def set_rating(self, user_id: str, item_id: str, rating: float) -> None:
        self._post(f"Users/{user_id}/Items/{item_id}/Rating", params={"rating": rating})

    def mark_favorite(self, user_id: str, item_id: str) -> None:
        self._post(f"Users/{user_id}/FavoriteItems/{item_id}")
```

- [ ] **Step 4: Run tests — verify they all PASS**

```bash
pytest test_jellyfin_client.py -v
```

Expected: all green, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add jellyfin_client.py test_jellyfin_client.py
git commit -m "feat: rewrite jellyfin_client with auth header, pagination, user CRUD, ratings"
```

---

## Task 4: Add `user_manager.py` (TDD)

**Files:**
- Create: `test_user_manager.py`
- Create: `user_manager.py`

- [ ] **Step 1: Create `test_user_manager.py`**

```python
import pytest
from unittest.mock import MagicMock, patch

from models import PlexUser, JellyfinUser
from jellyfin_client import JellyFinServer
from user_manager import discover_plex_users, resolve_jellyfin_user


# --- discover_plex_users ---

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

        assert len(users) == 1  # only admin; broken user skipped
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


# --- resolve_jellyfin_user ---

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
```

- [ ] **Step 2: Run tests — verify they FAIL**

```bash
pytest test_user_manager.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'user_manager'`

- [ ] **Step 3: Create `user_manager.py`**

```python
import secrets
import string
from typing import List, Optional

from loguru import logger
from plexapi.server import PlexServer

from jellyfin_client import JellyFinServer
from models import PlexUser, JellyfinUser


def discover_plex_users(plex: PlexServer, base_token: str) -> List[PlexUser]:
    account = plex.myPlexAccount()
    users: List[PlexUser] = [PlexUser(name=account.title, token=base_token, is_managed=False)]

    for managed in account.users():
        try:
            token = managed.get_token(plex.machineIdentifier)
            if not token:
                logger.warning(f"Could not get token for Plex user '{managed.title}' — skipping")
                continue
            users.append(PlexUser(name=managed.title, token=token, is_managed=True))
        except Exception as e:
            logger.warning(f"Failed to get token for Plex user '{managed.title}': {e} — skipping")

    return users


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def resolve_jellyfin_user(
    jf: JellyFinServer,
    plex_name: str,
    auto_create: bool,
    dry_run: bool,
) -> Optional[JellyfinUser]:
    jf_users = jf.get_users()

    # Exact match first
    for u in jf_users:
        if u.name == plex_name:
            return u

    # Case-insensitive fallback
    for u in jf_users:
        if u.name.lower() == plex_name.lower():
            logger.info(f"Matched Plex user '{plex_name}' → Jellyfin user '{u.name}' (case-insensitive)")
            return u

    if not auto_create:
        logger.warning(
            f"No Jellyfin user found for '{plex_name}' — skipping "
            f"(pass --auto-create-user to create automatically)"
        )
        return None

    if dry_run:
        logger.info(f"Would create Jellyfin user '{plex_name}' (dry run)")
        return None

    password = _random_password()
    user = jf.create_user(plex_name, password)
    print(f"\n  ✔ Created Jellyfin user '{user.name}' — initial password: {password}\n")
    logger.info(f"Created Jellyfin user '{user.name}'")
    return user
```

- [ ] **Step 4: Run tests — verify they all PASS**

```bash
pytest test_user_manager.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add user_manager.py test_user_manager.py
git commit -m "feat: add user_manager — Plex user discovery and Jellyfin user matching/creation"
```

---

## Task 5: Refactor `migrate.py` (TDD)

**Files:**
- Create: `test_migrate.py`
- Rewrite: `migrate.py`

- [ ] **Step 1: Create `test_migrate.py`**

```python
import pytest
from unittest.mock import MagicMock, patch, call
from plexapi import library as plex_library

from models import JellyfinUser, MigrationStats
from jellyfin_client import JellyFinServer
from migrate import migrate_user, PathTranslation, build_translation_library, translate_path


def make_plex_movie(file_path: str, user_rating=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    movie = MagicMock()
    movie.media = [medium]
    movie.userRating = user_rating
    return movie


def make_plex_episode(file_path: str, user_rating=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    ep = MagicMock()
    ep.media = [medium]
    ep.userRating = user_rating
    return ep


def make_jf_item(item_id: str, path: str, played=False, name="Test Item"):
    return {
        "Id": item_id,
        "Name": name,
        "MediaSources": [{"Path": path}],
        "UserData": {"Played": played},
    }


def make_plex(sections):
    plex = MagicMock()
    plex.library.sections.return_value = sections
    return plex


def make_movie_section(movies):
    section = MagicMock(spec=plex_library.MovieSection)
    section.title = "Movies"
    section.search.return_value = movies
    return section


def make_show_section(shows):
    section = MagicMock(spec=plex_library.ShowSection)
    section.title = "TV Shows"
    section.searchShows.return_value = shows
    return section


@pytest.fixture
def jf():
    client = MagicMock(spec=JellyFinServer)
    return client


@pytest.fixture
def jf_user():
    return JellyfinUser(id="jf_uid", name="alice")


class TestMigrateUserMovies:
    def test_marks_unwatched_item(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv")
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_called_once_with(user_id="jf_uid", item_id="jf1")
        assert stats.marked == 1

    def test_skips_already_watched_item(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv")
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=True)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_not_called()
        assert stats.skipped == 1

    def test_dry_run_does_not_mark_watched(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv")
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=True, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_not_called()
        assert stats.marked == 1

    def test_counts_missing_when_no_jf_match(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv")
        jf.iter_items.return_value = iter([])  # nothing in Jellyfin
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        assert stats.missing == 1
        jf.mark_watched.assert_not_called()

    def test_applies_path_translation(self, jf, jf_user):
        movie = make_plex_movie("/plex/film.mkv")
        jf_item = make_jf_item("jf1", "/jf/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])
        translations = build_translation_library(["/plex|/jf"])

        stats = migrate_user(plex, jf, jf_user, translations, dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_called_once()
        assert stats.marked == 1


class TestMigrateUserRatings:
    def test_sets_rating_when_flag_enabled(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", user_rating=7.0)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=True, migrate_favorites=False)

        jf.set_rating.assert_called_once_with("jf_uid", "jf1", 7.0)
        assert stats.ratings_set == 1

    def test_does_not_set_rating_when_flag_disabled(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", user_rating=7.0)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                     migrate_ratings=False, migrate_favorites=False)

        jf.set_rating.assert_not_called()

    def test_marks_favorite_when_rating_gte_9(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", user_rating=9.0)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=True)

        jf.mark_favorite.assert_called_once_with("jf_uid", "jf1")
        assert stats.favorites_set == 1

    def test_does_not_mark_favorite_when_rating_lt_9(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", user_rating=8.0)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                     migrate_ratings=False, migrate_favorites=True)

        jf.mark_favorite.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they FAIL**

```bash
pytest test_migrate.py -v 2>&1 | head -20
```

Expected: import errors or failures — `migrate_user` not found.

- [ ] **Step 3: Rewrite `migrate.py`**

```python
#!/usr/bin/env python3
from typing import List, NamedTuple, Set, Optional
import sys

import requests
import urllib3
import click
from loguru import logger
from tqdm import tqdm
from plexapi.server import PlexServer
from plexapi import library
from plexapi.media import Media
from urllib.parse import urlparse

from jellyfin_client import JellyFinServer, JellyfinAPIError
from user_manager import discover_plex_users, resolve_jellyfin_user
from models import PlexUser, JellyfinUser, MigrationStats


LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<level>{message}</level> | "
    "{extra}"
)


class PathTranslation(NamedTuple):
    src: str
    dst: str


TranslationLib = List[PathTranslation]


def build_translation_library(args: List[str]) -> TranslationLib:
    translations: TranslationLib = []
    for arg in args:
        src, dst = arg.split("|", 1)
        translations.append(PathTranslation(src=src, dst=dst))
    return translations


def translate_path(path: str, translations: TranslationLib) -> str:
    tr_path = path
    for t in translations:
        if tr_path.startswith(t.src):
            tr_path = t.dst + tr_path[len(t.src):]
            if "/" in t.dst and "\\" in tr_path:
                tr_path = tr_path.replace("\\", "/")
            elif "\\" in t.dst and "/" in tr_path:
                tr_path = tr_path.replace("/", "\\")
    return tr_path


def _watch_parts(media: List[Media]) -> Set[str]:
    watched: Set[str] = set()
    for medium in media:
        watched.update(p.file for p in medium.parts)
    return watched


def _validate_url(ctx, param, value):
    if value is None:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise click.BadParameter(f"must be a valid HTTP/HTTPS URL, got: {value!r}")
    return value


def _load_config_callback(ctx, param, value):
    if not value:
        return value
    import yaml
    with open(value) as f:
        raw = yaml.safe_load(f) or {}
    plex = raw.get("plex", {})
    jf = raw.get("jellyfin", {})
    opts = raw.get("options", {})
    mapping = {
        "plex_url": plex.get("url"),
        "plex_token": plex.get("token"),
        "jellyfin_url": jf.get("url"),
        "jellyfin_token": jf.get("token"),
        "all_users": opts.get("all_users"),
        "auto_create_user": opts.get("auto_create_user"),
        "dry_run": opts.get("dry_run"),
        "migrate_ratings": opts.get("migrate_ratings"),
        "migrate_favorites": opts.get("migrate_favorites"),
        "secure": opts.get("secure"),
        "translate": raw.get("translations", []) or [],
    }
    ctx.default_map = {k: v for k, v in mapping.items() if v is not None}
    return value


def migrate_user(
    plex: PlexServer,
    jf: JellyFinServer,
    jf_user: JellyfinUser,
    translations: TranslationLib,
    dry_run: bool,
    no_skip: bool,
    migrate_ratings: bool,
    migrate_favorites: bool,
    bulk_mode: bool = False,
) -> MigrationStats:
    stats = MigrationStats()
    track_item_meta = migrate_ratings or migrate_favorites

    # Build Jellyfin path index
    logger.info(f"Loading Jellyfin library for '{jf_user.name}'...")
    jf_entries: dict = {}
    for item in tqdm(jf.iter_items(jf_user.id), desc="Jellyfin items", unit=" item", leave=False):
        for source in item.get("MediaSources", []):
            path = source.get("Path")
            if not path:
                continue
            jf_entries.setdefault(path, []).append(item)
            logger.bind(path=path, id=item["Id"]).debug("jf entry")

    # Load Plex watched items
    logger.info(f"Loading Plex watched items...")
    plex_watched: Set[str] = set()
    plex_item_meta: dict = {}

    for section in plex.library.sections():
        if isinstance(section, library.MovieSection):
            for m in tqdm(section.search(unwatched=False), desc=f"Movies ({section.title})", unit=" movie", leave=False):
                parts = _watch_parts(m.media)
                plex_watched.update(parts)
                if track_item_meta:
                    for p in parts:
                        plex_item_meta[p] = {"userRating": getattr(m, "userRating", None)}

        elif isinstance(section, library.ShowSection):
            try:
                shows = section.searchShows(**{"episode.unwatched": False})
            except Exception:
                logger.warning(
                    f"PlexAPI episode filter not supported for '{section.title}' — "
                    f"falling back to client-side filter"
                )
                shows = [s for s in section.searchShows() if s.watched()]

            for show in tqdm(shows, desc=f"TV ({section.title})", unit=" show", leave=False):
                for ep in show.watched():
                    parts = _watch_parts(ep.media)
                    plex_watched.update(parts)
                    if track_item_meta:
                        for p in parts:
                            plex_item_meta[p] = {"userRating": getattr(ep, "userRating", None)}

        else:
            logger.info(
                f"Section '{section.title}' ({type(section).__name__}) is not supported — skipped"
            )

    # Match and migrate
    for watched in tqdm(plex_watched, desc="Migrating", unit=" item", leave=False):
        tr_watched = translate_path(watched, translations)

        if tr_watched not in jf_entries:
            logger.bind(path=tr_watched).warning(
                "No match in Jellyfin — check paths or use --translate to remap"
            )
            stats.missing += 1
            if no_skip:
                if bulk_mode:
                    logger.error(f"--no-skip: missing match for '{tr_watched}', marking user migration failed")
                else:
                    sys.exit(1)
            continue

        for jf_entry in jf_entries[tr_watched]:
            user_data = jf_entry.get("UserData", {})
            item_id = jf_entry["Id"]
            item_name = jf_entry["Name"]

            if not user_data.get("Played"):
                stats.marked += 1
                if not dry_run:
                    try:
                        jf.mark_watched(user_id=jf_user.id, item_id=item_id)
                        logger.bind(path=tr_watched, jf_id=item_id, title=item_name).info("Marked as watched")
                    except JellyfinAPIError as e:
                        logger.error(f"Failed to mark '{item_name}' as watched: {e}")
                else:
                    logger.bind(path=tr_watched, jf_id=item_id, title=item_name).info("Would be marked as watched (dry run)")
            else:
                stats.skipped += 1
                logger.bind(path=tr_watched, jf_id=item_id, title=item_name).debug("Already watched — skipped")

            meta = plex_item_meta.get(tr_watched, {})
            user_rating = meta.get("userRating")

            if migrate_ratings and user_rating is not None:
                stats.ratings_set += 1
                if not dry_run:
                    try:
                        jf.set_rating(jf_user.id, item_id, user_rating)
                    except JellyfinAPIError as e:
                        logger.error(f"Failed to set rating for '{item_name}': {e}")

            if migrate_favorites and user_rating is not None and user_rating >= 9:
                stats.favorites_set += 1
                if not dry_run:
                    try:
                        jf.mark_favorite(jf_user.id, item_id)
                    except JellyfinAPIError as e:
                        logger.error(f"Failed to mark '{item_name}' as favorite: {e}")

    return stats


@click.command()
@click.option("--config", type=click.Path(exists=True), is_eager=True, expose_value=False,
              callback=_load_config_callback, help="YAML config file (values overridden by CLI flags)")
@click.option("--plex-url", required=True, callback=_validate_url, help="Plex server URL")
@click.option("--plex-token", required=True, help="Plex token")
@click.option("--plex-managed-user", help="Specific managed user (single-user mode only)")
@click.option("--jellyfin-url", required=True, callback=_validate_url, help="Jellyfin server URL")
@click.option("--jellyfin-token", required=True, help="Jellyfin API token")
@click.option("--jellyfin-user", default=None, help="Jellyfin username (required without --all-users)")
@click.option("--all-users", is_flag=True, default=False, help="Migrate all Plex users in one run")
@click.option("--auto-create-user/--no-auto-create-user", default=None,
              help="Create missing Jellyfin accounts (default: on with --all-users)")
@click.option("--translate", type=str, multiple=True, default=[],
              help="Path translation SRC|DST (repeatable)")
@click.option("--migrate-ratings/--no-migrate-ratings", default=False,
              help="Migrate Plex ratings to Jellyfin")
@click.option("--migrate-favorites/--no-migrate-favorites", default=False,
              help="Migrate highly-rated Plex items (≥9) as Jellyfin favorites")
@click.option("--secure/--insecure", default=False, help="Verify SSL certificates")
@click.option("--debug/--no-debug", default=False, help="Verbose debug logging")
@click.option("--no-skip/--skip", default=False, help="Exit (or fail user) on unmatched paths")
@click.option("--dry-run", is_flag=True, default=False, help="Preview without writing to Jellyfin")
def migrate(plex_url, plex_token, plex_managed_user, jellyfin_url, jellyfin_token,
            jellyfin_user, all_users, auto_create_user, translate, migrate_ratings,
            migrate_favorites, secure, debug, no_skip, dry_run):
    logger.remove()
    logger.add(sys.stderr, format=LOG_FORMAT, level="DEBUG" if debug else "INFO")

    if not secure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not all_users and not jellyfin_user:
        raise click.UsageError("--jellyfin-user is required when --all-users is not set")

    if auto_create_user is None:
        auto_create_user = all_users

    session = requests.Session()
    session.verify = secure
    plex = PlexServer(plex_url, plex_token, session=session)

    if plex_managed_user and not all_users:
        managed = plex.myPlexAccount().user(plex_managed_user)
        managed_token = managed.get_token(plex.machineIdentifier)
        plex = PlexServer(plex_url, managed_token, session=session)

    jf = JellyFinServer(url=jellyfin_url, api_key=jellyfin_token, session=session)
    translations = build_translation_library(list(translate))

    if all_users:
        plex_users = discover_plex_users(plex, plex_token)
        all_stats: dict = {}

        for plex_user in plex_users:
            logger.info(f"Processing Plex user '{plex_user.name}'...")
            jf_user = resolve_jellyfin_user(jf, plex_user.name, auto_create_user, dry_run)
            if not jf_user:
                continue
            scoped_plex = PlexServer(plex_url, plex_user.token, session=session)
            try:
                stats = migrate_user(scoped_plex, jf, jf_user, translations,
                                     dry_run, no_skip, migrate_ratings, migrate_favorites,
                                     bulk_mode=True)
                all_stats[plex_user.name] = (jf_user, stats)
            except Exception as e:
                logger.error(f"Migration failed for '{plex_user.name}': {e}")

        _print_bulk_summary(all_stats, dry_run)
    else:
        jf_users = jf.get_users()
        jf_user = next((u for u in jf_users if u.name == jellyfin_user), None)
        if not jf_user:
            jf_user = next((u for u in jf_users if u.name.lower() == jellyfin_user.lower()), None)
        if not jf_user:
            raise click.ClickException(f"Jellyfin user '{jellyfin_user}' not found")

        stats = migrate_user(plex, jf, jf_user, translations,
                             dry_run, no_skip, migrate_ratings, migrate_favorites)
        action = "Would migrate" if dry_run else "Successfully migrated"
        logger.bind(marked=stats.marked, missing=stats.missing, skipped=stats.skipped,
                    ratings=stats.ratings_set, favorites=stats.favorites_set).success(
            f"{action} watched states for '{jellyfin_user}'"
        )


def _print_bulk_summary(all_stats: dict, dry_run: bool) -> None:
    action = "Would migrate" if dry_run else "Migration complete"
    print(f"\n{action} — summary:\n")
    header = f"{'User':<20} {'Marked':>7} {'Missing':>8} {'Skipped':>8} {'Ratings':>8} {'Favorites':>10}"
    print(header)
    print("-" * len(header))
    for plex_name, (jf_user, stats) in all_stats.items():
        label = plex_name
        if jf_user.name != plex_name:
            label = f"{plex_name} → {jf_user.name}"
        print(f"{label:<20} {stats.marked:>7} {stats.missing:>8} {stats.skipped:>8} "
              f"{stats.ratings_set:>8} {stats.favorites_set:>10}")
    print()


if __name__ == "__main__":
    migrate()
```

- [ ] **Step 4: Run all tests — verify existing translate tests still pass and new tests pass**

```bash
pytest test_translate.py test_migrate.py -v
```

Expected: all green (test_translate.py imports `PathTranslation`, `build_translation_library`, `translate_path` from `migrate` — these are preserved).

- [ ] **Step 5: Commit**

```bash
git add migrate.py test_migrate.py
git commit -m "feat: refactor migrate.py — migrate_user(), bulk --all-users, ratings, favorites, tqdm"
```

---

## Task 6: Config File Support (TDD)

**Files:**
- Create: `test_config.py`
- Create: `config.example.yml`

The config loading logic is already implemented in `migrate.py` (`_load_config_callback`). This task adds tests and the example config file.

- [ ] **Step 1: Create `test_config.py`**

```python
import os
import pytest
import yaml
import tempfile
from click.testing import CliRunner

from migrate import migrate


def write_config(tmp_path, data: dict) -> str:
    path = os.path.join(tmp_path, "config.yml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


class TestConfigFile:
    def test_config_provides_plex_url(self, tmp_path):
        config_path = write_config(tmp_path, {
            "plex": {"url": "http://plex.local", "token": "pt"},
            "jellyfin": {"url": "http://jf.local", "token": "jt"},
        })
        runner = CliRunner()
        result = runner.invoke(migrate, ["--config", config_path, "--jellyfin-user", "alice", "--dry-run"])
        # Should fail connecting, not due to missing --plex-url
        assert "--plex-url" not in result.output

    def test_cli_overrides_config(self, tmp_path):
        config_path = write_config(tmp_path, {
            "plex": {"url": "http://wrong.local", "token": "pt"},
            "jellyfin": {"url": "http://jf.local", "token": "jt"},
        })
        runner = CliRunner()
        result = runner.invoke(migrate, [
            "--config", config_path,
            "--plex-url", "http://correct.local",
            "--jellyfin-user", "alice",
            "--dry-run",
        ])
        # --plex-url from CLI should override config; no "Missing option --plex-url" error
        assert "Missing option '--plex-url'" not in result.output

    def test_translations_loaded_from_config(self, tmp_path):
        config_path = write_config(tmp_path, {
            "plex": {"url": "http://p.local", "token": "pt"},
            "jellyfin": {"url": "http://j.local", "token": "jt"},
            "translations": ["/plex|/jf"],
        })
        runner = CliRunner()
        # Just verifying the config parses without error
        result = runner.invoke(migrate, ["--config", config_path, "--jellyfin-user", "u", "--dry-run"])
        assert "Error: Invalid value for '--config'" not in result.output
```

- [ ] **Step 2: Run tests — verify they behave as expected**

```bash
pytest test_config.py -v
```

Expected: tests may show connection errors (no real server) but should NOT show "Missing option" errors — config is being read.

- [ ] **Step 3: Create `config.example.yml`**

```yaml
# migrate-plex-to-jellyfin configuration file
# Copy to config.yml and fill in your values.
# All values here can be overridden by CLI flags.

plex:
  url: https://plex.yourdomain.com
  token: YOUR_PLEX_TOKEN_HERE

jellyfin:
  url: https://jellyfin.yourdomain.com
  token: YOUR_JELLYFIN_API_KEY_HERE

options:
  # Migrate all Plex users in one run
  all_users: true

  # Create Jellyfin accounts for Plex users who don't have one yet
  auto_create_user: true

  # Preview what would happen without writing anything
  dry_run: false

  # Migrate Plex star ratings to Jellyfin
  migrate_ratings: false

  # Mark items rated ≥9 in Plex as Jellyfin favorites
  migrate_favorites: false

  # Verify SSL certificates (set false for self-signed certs)
  secure: false

# Path translations: Plex path prefix | Jellyfin path prefix
# Needed when Plex and Jellyfin mount the same media at different paths.
# Leave empty if paths match (common in Saltbox setups).
translations: []
# translations:
#   - "/mnt/plex/Media|/data/Media"
```

- [ ] **Step 4: Commit**

```bash
git add test_config.py config.example.yml
git commit -m "feat: add config file support and config.example.yml"
```

---

## Task 7: Docker & Deployment Files

**Files:**
- Rewrite: `dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `docker-compose.saltbox.yml`
- Create: `.env.example`

- [ ] **Step 1: Rewrite `dockerfile`**

```dockerfile
# ---- build stage: install deps ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY models.py jellyfin_client.py user_manager.py migrate.py ./

ENTRYPOINT ["python3", "migrate.py"]
```

- [ ] **Step 2: Create `.dockerignore`**

```
.git
.github
docs
plexsync
venv
__pycache__
*.pyc
*.pyo
*.md
test_*.py
config.example.yml
docker-compose*.yml
.env*
```

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  migrate:
    image: ghcr.io/wilmardo/migrate-plex-to-jellyfin:latest
    env_file:
      - .env
    volumes:
      - ./config.yml:/app/config.yml:ro
    command: --config /app/config.yml
    restart: "no"
```

- [ ] **Step 4: Create `docker-compose.saltbox.yml`**

```yaml
# Saltbox variant — connects to the 'saltbox' Docker network so Plex and
# Jellyfin containers are reachable by their container names.
#
# Saltbox default paths:
#   Plex media:     /mnt/unionfs/Media  →  container path: /data/Media
#   Jellyfin media: /mnt/unionfs/Media  →  container path: /data/Media
#
# Because both containers use the same internal path (/data/...), no
# --translate flag is needed unless you've customised your mount points.
#
# Usage:
#   1. Copy .env.example → .env and fill in your tokens
#   2. Copy config.example.yml → config.yml and set all_users: true
#   3. docker compose -f docker-compose.saltbox.yml run --rm migrate

services:
  migrate:
    image: ghcr.io/wilmardo/migrate-plex-to-jellyfin:latest
    networks:
      - saltbox
    env_file:
      - .env
    volumes:
      - ./config.yml:/app/config.yml:ro
    command: --config /app/config.yml
    restart: "no"

networks:
  saltbox:
    external: true
```

- [ ] **Step 5: Create `.env.example`**

```env
# Copy this file to .env and fill in your values.
# These are used by docker-compose; you can also set them in config.yml.

# Plex server URL (e.g. https://plex.yourdomain.com or http://plex:32400)
PLEX_URL=https://plex.yourdomain.com
PLEX_TOKEN=your_plex_token_here

# Jellyfin server URL (e.g. https://jellyfin.yourdomain.com or http://jellyfin:8096)
JELLYFIN_URL=https://jellyfin.yourdomain.com
JELLYFIN_TOKEN=your_jellyfin_api_key_here
```

- [ ] **Step 6: Build the Docker image and verify**

```bash
docker build -t migrate-plex-to-jellyfin:local .
docker run --rm migrate-plex-to-jellyfin:local --help
```

Expected: `--help` output shows all flags including `--all-users`, `--migrate-ratings`, etc.

- [ ] **Step 7: Commit**

```bash
git add dockerfile .dockerignore docker-compose.yml docker-compose.saltbox.yml .env.example
git commit -m "feat: multi-stage Dockerfile, generic and Saltbox docker-compose, .env.example"
```

---

## Task 8: GitHub Actions

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `.github/workflows/publish.yml`

- [ ] **Step 1: Create `.github/workflows/test.yml`**

```yaml
name: Tests

on:
  push:
    branches: ["**"]
  pull_request:
    branches: ["**"]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements.dev.txt

      - name: Run tests
        run: pytest -v
```

- [ ] **Step 2: Create `.github/workflows/publish.yml`**

```yaml
name: Publish Docker image

on:
  push:
    tags:
      - "v*"

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=raw,value=latest

      - name: Set up QEMU (multi-platform)
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

- [ ] **Step 3: Commit**

```bash
mkdir -p .github/workflows
git add .github/workflows/test.yml .github/workflows/publish.yml
git commit -m "ci: add pytest matrix workflow and GHCR publish on version tags"
```

---

## Task 9: README Overhaul

**Files:**
- Rewrite: `README.md`

- [ ] **Step 1: Rewrite `README.md`**

```markdown
# migrate-plex-to-jellyfin

Migrate Plex watched states, ratings, and favorites to Jellyfin.  
Supports bulk migration of all users, auto Jellyfin account creation, and path translation for different mount points.

---

## Quick Start (Docker — recommended)

```bash
# 1. Copy the example files
cp .env.example .env
cp config.example.yml config.yml

# 2. Fill in your tokens (see token guides below)
#    .env    → PLEX_URL, PLEX_TOKEN, JELLYFIN_URL, JELLYFIN_TOKEN
#    config.yml → options (all_users, dry_run, etc.)

# 3. Dry run first — nothing is written to Jellyfin
docker compose run --rm migrate --dry-run

# 4. Run for real
docker compose run --rm migrate
```

Pre-built images are available at `ghcr.io/wilmardo/migrate-plex-to-jellyfin`.

---

## Saltbox Setup

Saltbox users can use the dedicated compose file, which connects to the `saltbox` Docker network so Plex and Jellyfin are reachable by container name. Because both containers typically mount media at `/data/Media/`, no path translation is needed.

```bash
docker compose -f docker-compose.saltbox.yml run --rm migrate --dry-run
docker compose -f docker-compose.saltbox.yml run --rm migrate
```

In `config.yml`, set your Plex/Jellyfin URLs to the internal container names if they're on the same Docker host:

```yaml
plex:
  url: http://plex:32400
jellyfin:
  url: http://jellyfin:8096
```

---

## Getting Tokens

- **Plex token:** https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/
- **Jellyfin token:** Dashboard → API Keys → New API Key

---

## Configuration File

Copy `config.example.yml` to `config.yml` and edit it.  
CLI flags always override config file values.

```yaml
plex:
  url: https://plex.yourdomain.com
  token: YOUR_PLEX_TOKEN

jellyfin:
  url: https://jellyfin.yourdomain.com
  token: YOUR_JELLYFIN_API_KEY

options:
  all_users: true          # migrate all Plex users in one run
  auto_create_user: true   # create Jellyfin account if user not found
  dry_run: false
  migrate_ratings: false   # copy Plex star ratings to Jellyfin
  migrate_favorites: false # items rated ≥9 in Plex → Jellyfin favorite
  secure: false            # set true for verified SSL

translations: []           # see Path Translation below
```

---

## CLI Reference

```
python3 migrate.py [OPTIONS]

Core:
  --config PATH                  YAML config file
  --plex-url TEXT                Plex server URL  [required]
  --plex-token TEXT              Plex token  [required]
  --plex-managed-user TEXT       Specific managed user (single-user mode)
  --jellyfin-url TEXT            Jellyfin server URL  [required]
  --jellyfin-token TEXT          Jellyfin API key  [required]
  --jellyfin-user TEXT           Jellyfin username (required without --all-users)

Users:
  --all-users                    Migrate all Plex users in one run
  --auto-create-user / --no-auto-create-user
                                 Create missing Jellyfin accounts (default on with --all-users)

Migration options:
  --migrate-ratings / --no-migrate-ratings
                                 Copy Plex star ratings to Jellyfin
  --migrate-favorites / --no-migrate-favorites
                                 Mark items rated ≥9 in Plex as Jellyfin favorites
  --translate SRC|DST            Path translation (repeatable)

Behaviour:
  --secure / --insecure          Verify SSL (default: insecure)
  --debug / --no-debug           Verbose output
  --no-skip / --skip             Fail on unmatched paths (default: skip)
  --dry-run                      Preview without writing to Jellyfin
  --help                         Show this message and exit
```

### Single user example

```bash
python3 migrate.py \
  --plex-url https://plex.example.com \
  --plex-token abc123 \
  --jellyfin-url https://jellyfin.example.com \
  --jellyfin-token xyz789 \
  --jellyfin-user john \
  --dry-run
```

### All users with auto-create

```bash
python3 migrate.py \
  --plex-url https://plex.example.com --plex-token abc123 \
  --jellyfin-url https://jellyfin.example.com --jellyfin-token xyz789 \
  --all-users --auto-create-user \
  --migrate-ratings --migrate-favorites \
  --dry-run
```

---

## Path Translation

If Plex and Jellyfin mount the same media at different paths, use `--translate SRC|DST` (repeatable) or set `translations:` in `config.yml`.

```bash
# Plex sees /media/... but Jellyfin sees /mnt/media/...
--translate "/media|/mnt/media"

# Windows Plex → Linux Jellyfin
--translate 'D:\Media|/data/media'
```

---

## Local Installation (without Docker)

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 migrate.py --help
```

---

## Running Tests

```bash
pip install -r requirements.dev.txt
pytest -v
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: overhaul README with all-users, config file, Docker Compose, Saltbox, CLI reference"
```

---

## Self-Review Checklist

### Spec coverage

| Spec section | Task |
|---|---|
| models.py dataclasses | Task 1 |
| Jellyfin 10.9+ auth header | Task 3 |
| Pagination generator | Task 3 |
| mark_watched response validation (#16) | Task 3 |
| get_users typed list | Task 3 |
| create_user + set_rating + mark_favorite | Task 3 |
| Dead code removed (search_by_provider, get_user_views, get_user_id) | Task 3 |
| discover_plex_users (main + managed + home users, .title not .username) | Task 4 |
| resolve_jellyfin_user (exact → CI → create) | Task 4 |
| Random 16-char password printed to stdout | Task 4 |
| migrate_user() extracted function | Task 5 |
| --all-users bulk mode | Task 5 |
| Ratings + favorites migration | Task 5 |
| tqdm progress bars | Task 5 |
| Music section notice (not silent skip) | Task 5 |
| URL validation at startup (#20) | Task 5 |
| PlexAPI episode filter fallback (#10) | Task 5 |
| --no-skip bug fix (single + bulk mode) | Task 5 |
| --auto-create-user flag | Task 5 |
| Bulk summary table | Task 5 |
| HTTP error handling in _get/_post | Task 3 |
| JSON decode error hint (#33) | Task 3 |
| Per-user error isolation in bulk mode | Task 5 |
| Config file support (--config) | Task 6 |
| config.example.yml | Task 6 |
| requirements tqdm + PyYAML | Task 2 |
| pytest-mock | Task 2 |
| Dockerfile multi-stage + pinned Python | Task 7 |
| .dockerignore | Task 7 |
| docker-compose.yml | Task 7 |
| docker-compose.saltbox.yml (saltbox network) | Task 7 |
| .env.example | Task 7 |
| GitHub Actions test workflow | Task 8 |
| GitHub Actions GHCR publish workflow | Task 8 |
| README overhaul | Task 9 |

All spec items covered. No gaps found.

### Type consistency check

- `JellyfinUser` defined in Task 1, used in Tasks 3, 4, 5 — consistent
- `PlexUser` defined in Task 1, used in Tasks 4, 5 — consistent
- `MigrationStats` defined in Task 1, used in Task 5 — consistent
- `migrate_user()` signature defined in Task 5 step 3 — test in step 1 matches signature
- `JellyfinAPIError(status_code, message)` defined in Task 3 — used in Task 5 — consistent
- `iter_items(user_id, page_size)` defined in Task 3 — called in Task 5 — consistent
- `mark_watched(user_id, item_id)` — Task 3 definition, Task 5 call: `jf.mark_watched(user_id=jf_user.id, item_id=item_id)` — consistent
- `set_rating(user_id, item_id, rating)` — Task 3 definition, Task 5 call: `jf.set_rating(jf_user.id, item_id, user_rating)` — consistent
- `mark_favorite(user_id, item_id)` — Task 3 definition, Task 5 call: `jf.mark_favorite(jf_user.id, item_id)` — consistent
- `_post(endpoint, params, body)` — Task 3 definition, Task 3 internal usage — consistent
- `build_translation_library` / `translate_path` / `PathTranslation` — unchanged from original, preserved in Task 5 for test_translate.py compat — consistent
