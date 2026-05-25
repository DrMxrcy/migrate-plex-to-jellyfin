# migrate-plex-to-jellyfin

Migrate Plex watched states, ratings, favorites, and last-viewed timestamps to Jellyfin.
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
  migrate_timestamps: true # copy lastViewedAt to Jellyfin DatePlayed
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
  --migrate-timestamps / --no-migrate-timestamps
                                 Copy Plex lastViewedAt to Jellyfin DatePlayed (default: on)
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
  --migrate-ratings --migrate-favorites --migrate-timestamps \
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
