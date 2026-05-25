# migrate-plex-to-jellyfin

Migrate Plex watched states, playback resume positions, ratings, favorites, and last-viewed timestamps to Jellyfin.
Supports bulk migration of all users, auto Jellyfin account creation, and path translation for different mount points.

---

## Quick Start (Saltbox Docker — recommended)

```bash
# 1. Clone the project into Saltbox's /opt app folder
sudo git clone https://github.com/DrMxrcy/migrate-plex-to-jellyfin.git /opt/migrate-plex-to-jellyfin
sudo chown -R 1000:1000 /opt/migrate-plex-to-jellyfin
cd /opt/migrate-plex-to-jellyfin

# 2. Use the Saltbox compose file and create your config
cp docker-compose.saltbox.yml docker-compose.yml
cp config.example.yml config.yml

# 3. Fill in your Plex and Jellyfin tokens
#    Saltbox defaults use http://plex:32400 and http://jellyfin:8096
nano config.yml

# 4. Dry run first — nothing is written to Jellyfin
docker compose run --rm migrate --dry-run

# 5. Run for real when the dry run looks right
docker compose run --rm migrate
```

Docker only needs `config.yml` for this project. You do not need both a `.env` file and a YAML config.

The Saltbox compose file pulls `ghcr.io/drmxrcy/migrate-plex-to-jellyfin:latest`.
GitHub Actions publishes GHCR images on every commit; branch and SHA tags are created for all pushed branches, and `latest` is updated from the default branch.

---

## Saltbox Setup

The Saltbox compose file is intentionally simple and follows the usual `/opt/<app>` pattern. It runs as `1000:1000`, uses the external `saltbox` network, stores config under `/opt/migrate-plex-to-jellyfin`, mounts `/mnt`, and marks the service with the Saltbox managed label.

In `/opt/migrate-plex-to-jellyfin/config.yml`, keep the Plex/Jellyfin URLs on the internal container names:

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
  url: http://plex:32400
  token: YOUR_PLEX_TOKEN

jellyfin:
  url: http://jellyfin:8096
  token: YOUR_JELLYFIN_API_KEY

options:
  all_users: true          # migrate all Plex users in one run
  auto_create_user: true   # create Jellyfin account if user not found
  dry_run: false
  migrate_ratings: false   # copy Plex star ratings to Jellyfin
  migrate_favorites: false # items rated ≥9 in Plex → Jellyfin favorite
  migrate_timestamps: true # copy lastViewedAt to Jellyfin DatePlayed
  migrate_positions: true  # copy viewOffset resume points to Jellyfin
  secure: false            # set true for verified SSL

user_mappings: {}          # see User Mapping below

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
  --user-map PLEX|JELLYFIN       Assign Plex user to existing Jellyfin user (repeatable)

Migration options:
  --migrate-ratings / --no-migrate-ratings
                                 Copy Plex star ratings to Jellyfin
  --migrate-favorites / --no-migrate-favorites
                                 Mark items rated ≥9 in Plex as Jellyfin favorites
  --migrate-timestamps / --no-migrate-timestamps
                                 Copy Plex lastViewedAt to Jellyfin DatePlayed (default: on)
  --migrate-positions / --no-migrate-positions
                                 Copy Plex viewOffset resume positions to Jellyfin (default: on)
  --translate SRC|DST            Path translation (repeatable)

Behaviour:
  --secure / --insecure          Verify SSL (default: insecure)
  --debug / --no-debug           Verbose output
  --no-skip / --skip             Fail on unmatched paths (default: skip)
  --dry-run                      Preview without writing to Jellyfin
  --help                         Show this message and exit
```

### User Mapping

Bulk mode first tries to match Plex users to Jellyfin users by exact name, then by case-insensitive name. If no Jellyfin user matches, `auto_create_user: true` creates a new Jellyfin account.

To assign a Plex user to an existing Jellyfin account instead, add `user_mappings`:

```yaml
user_mappings:
  "Gavin Snell (Gavin8tor245)": "gavin"
  "JP": "john"
```

The same mapping can be passed from the CLI:

```bash
python3 migrate.py --all-users --user-map "Gavin Snell (Gavin8tor245)|gavin"
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
  --migrate-ratings --migrate-favorites --migrate-timestamps --migrate-positions \
  --dry-run
```

### Generic Docker

If you are not running Saltbox, use the generic compose file and set public or LAN URLs in `config.yml`.

```bash
docker compose build
docker compose run --rm migrate --dry-run
docker compose run --rm migrate
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
