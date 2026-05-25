# Design: Jellyfin Migration v2

**Date:** 2026-05-24  
**Status:** Approved  

## Context

The current script (`migrate.py` + `jellyfin_client.py`) migrates Plex watched states to Jellyfin by matching file paths. It works for a single hardcoded Jellyfin user per run and has several known bugs including a non-functional `--no-skip` flag, no pagination (breaks on large libraries), JSON decode crashes on Jellyfin 10.9+, and a dead `search_by_provider` method with an undefined variable.

This redesign adds bulk multi-user migration with auto account creation, ratings/favorites migration, and fixes all known upstream issues — structured as a full refactor into four focused files.

---

## Goals

1. Bulk all-users mode: discover all Plex users (main + managed home users), auto-create missing Jellyfin accounts
2. Ratings and favorites migration (opt-in)
3. Fix all upstream bugs (issues #33, #32, #30, #28, #20, #10 + non-functional `--no-skip`)
4. Jellyfin 10.9+ API compatibility (auth header, pagination)
5. Typed, testable code split into focused modules

## Upstream Issue Coverage

| Issue | Summary | Resolution |
|-------|---------|------------|
| #33 | JSON decode error on Jellyfin 10.11+ | `_get` catches `JSONDecodeError`, logs version hint |
| #32 | Zero path matches despite identical files | Better warning suggests `--translate`; improved debug output |
| #30 | Hangs/OOM on large libraries (40k+ items) | `iter_items()` pagination generator (5000 items/page) |
| #28 | `KeyError: 'Path'` on some items | Already fixed by PR #29; guard retained in new code |
| #20 | `InvalidSchema` URL crash on Synology | URL validation at startup, clear error message |
| #10 | `Unknown filter category: unwatched` | PlexAPI fallback with `show.watched()` client-side filter |
| #7  | `NameError: name 'skip' is not defined` | Already fixed; `--no-skip` flag now actually exits on missing |
| #4  | Ratings and play history migration | `--migrate-ratings` / `--migrate-favorites` flags |
| #16 | Migration "succeeds" but nothing marked | `mark_watched` response validated; logs error on non-2xx |

---

## File Layout

```
models.py           NEW  — PlexUser, JellyfinUser, MigrationStats dataclasses
jellyfin_client.py  REV  — full rewrite: typed returns, pagination, user CRUD, ratings
user_manager.py     NEW  — Plex user discovery, Jellyfin user matching + auto-creation
migrate.py          REV  — CLI only + migrate_user() core function
test_translate.py        — unchanged
```

---

## Section 1: Models (`models.py`)

```python
@dataclass
class PlexUser:
    name: str
    token: str        # auth token scoped to this user on this server
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

---

## Section 2: Jellyfin Client (`jellyfin_client.py`)

### Auth
Send `Authorization: MediaBrowser Token="<key>", Client="migrate-plex-to-jellyfin", Version="2.0"` as a header on every request. Remove `?api_key=` query param pattern.

### HTTP helpers
- `_get(endpoint, params) -> dict` — validates HTTP status, catches `JSONDecodeError` with a clear message pointing to Jellyfin version mismatch. Raises `JellyfinAPIError` on failure.
- `_post(endpoint, params, json_body) -> requests.Response` — same validation, returns response so callers can inspect.
- `_delete(endpoint, params) -> requests.Response` — new, for unfavorite

### Pagination
```python
def iter_items(self, user_id: str, page_size: int = 5000) -> Iterator[dict]:
    start = 0
    while True:
        page = self._get("Users/{uid}/Items", params={
            "Recursive": True, "Fields": "MediaSources,UserData",
            "StartIndex": start, "Limit": page_size
        })
        items = page["Items"]
        yield from items
        if len(items) < page_size:
            break
        start += len(items)
```
This replaces `get_all()`.

### User methods
- `get_users() -> List[JellyfinUser]` — replaces the dict-returning version
- `create_user(name: str, password: str) -> JellyfinUser` — `POST /Users/New`, then `POST /Users/{id}/Password`

### Watch state
- `mark_watched(user_id: str, item_id: str)` — `POST /Users/{uid}/PlayedItems/{iid}`. Now checks response status and raises `JellyfinAPIError` on failure (previously silent — cause of issue #16).

### Ratings / favorites
- `set_rating(user_id: str, item_id: str, rating: float)` — `POST /Users/{uid}/Items/{iid}/Rating?rating={rating}`
- `mark_favorite(user_id: str, item_id: str)` — `POST /Users/{uid}/FavoriteItems/{iid}`

### Removed
- `search_by_provider` — dead code, referenced undefined `item_type`, not called anywhere
- `get_user_views` — not used
- `get_user_id` — replaced by callers using `get_users()` directly

---

## Section 3: User Manager (`user_manager.py`)

### `discover_plex_users(plex_server, base_token: str) -> List[PlexUser]`
1. Yields the main account as `PlexUser(name=plex.myPlexAccount().title, token=base_token, is_managed=False)`
2. Calls `plex.myPlexAccount().users()` — returns **both** managed users (child accounts) AND home users (shared Plex accounts). Both are `MyPlexUser` objects.
3. For each: `user.get_token(plex.machineIdentifier)` → `PlexUser(name=user.title, ...)`. Use `.title` not `.username` — home users may have no `.username`/`.email`.
4. Wraps PlexAPI errors with a clear log message; skips users whose token fetch fails

### `resolve_jellyfin_user(jf_client, plex_name: str, auto_create: bool, dry_run: bool) -> Optional[JellyfinUser]`
1. Fetch current Jellyfin user list
2. Try exact name match
3. Try case-insensitive match; log a notice if used
4. If no match and `auto_create=True` and not `dry_run`:
   - Generate random 16-char alphanumeric password
   - Call `jf_client.create_user(plex_name, password)`
   - Print: `Created Jellyfin user '{name}' — password: {password}` (stdout, prominent)
   - Return the new user
5. If no match and `dry_run`: log "Would create user '{name}'"
6. If no match and `auto_create=False`: log warning, return `None` (user is skipped)

---

## Section 4: CLI & Core Logic (`migrate.py`)

### New/changed flags

| Flag | Notes |
|------|-------|
| `--all-users` | New. Bulk mode — discovers all Plex users. Makes `--jellyfin-user` optional. |
| `--auto-create-user / --no-auto-create-user` | New. Default `True` in `--all-users` mode. |
| `--migrate-ratings / --no-migrate-ratings` | New. Default off. Migrates Plex userRating (1–10) → Jellyfin. |
| `--migrate-favorites / --no-migrate-favorites` | New. Default off. Items with `userRating >= 9` → Jellyfin favorite. |
| `--jellyfin-user` | Now optional (required only without `--all-users`). |
| All existing flags | Unchanged behaviour. |

### `migrate_user()` function
Extracted from the main command body:
```python
def migrate_user(
    plex_server: PlexServer,
    jf_client: JellyFinServer,
    jf_user: JellyfinUser,
    translations: TranslationLib,
    dry_run: bool,
    no_skip: bool,
    migrate_ratings: bool,
    migrate_favorites: bool,
) -> MigrationStats
```
Returns `MigrationStats`. The outer command calls this once or in a loop.

### `--no-skip` bug fix
When a path has no Jellyfin match, if `no_skip=True` (i.e. `--no-skip` flag passed):
- **Single-user mode:** log `ERROR` and `SystemExit(1)`
- **Bulk mode:** log `ERROR` for that item, mark that user's migration as failed, and continue to next user (aborting the entire multi-user run for one missing item would be too disruptive)
Currently this flag is silently ignored regardless of mode.

### Music library notice
When a `MusicSection` is encountered, log `INFO: Music section '{name}' is not supported — skipped` rather than silently ignoring it.

### URL validation
At startup, validate that `--plex-url` and `--jellyfin-url` are well-formed HTTP/HTTPS URLs and exit early with a clear message if not (prevents confusing `InvalidSchema` crashes).

### PlexAPI episode filter compatibility
Wrap `plex_tvshows.searchShows(**{"episode.unwatched": False})` in a try/except for `plexapi.exceptions.BadRequest`, falling back to `plex_tvshows.searchShows()` with client-side filtering of `show.watched()`. Log a warning when the fallback is used.

### Summary output (bulk mode)
After all users are processed, print a table:

```
User               Marked   Missing  Skipped  Ratings  Favorites
john               142      3        28       41       12
jane (new)         89       1        14       0        0
```

---

## Section 5: Error Handling

- HTTP errors → `JellyfinAPIError(status_code, message)` raised by client, caught per-user in bulk loop
- JSON decode error → caught in `_get`, logged as `ERROR` with hint: "Check Jellyfin version — this may indicate an API compatibility issue"
- Missing Plex managed user token → caught, logged, user skipped
- One user's failure in bulk mode does not abort remaining users

---

## Section 6: Testing

Existing `test_translate.py` is unchanged. New tests to add:

- `test_user_manager.py` — mock PlexAPI and JellyfinClient, test exact match / case-insensitive match / auto-create / dry-run paths
- `test_jellyfin_client.py` — mock `requests.Session`, test pagination stops correctly, tests error handling on non-2xx and bad JSON
- `test_migrate.py` — integration-style test of `migrate_user()` with mocked clients

Run with: `pytest`

---

## Verification

1. `python migrate.py --help` — verify all new flags appear
2. `python migrate.py --dry-run --all-users --plex-url ... --plex-token ... --jellyfin-url ... --jellyfin-token ...` — should enumerate all users and log what would happen without any writes
3. `python migrate.py --dry-run --migrate-ratings --migrate-favorites --plex-url ... --jellyfin-user ...` — single user with ratings dry run
4. `pytest` — all existing translate tests pass, new unit tests pass
5. `python migrate.py --no-skip ...` — verify exit code is non-zero when a path has no match
