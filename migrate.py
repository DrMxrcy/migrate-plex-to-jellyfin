#!/usr/bin/env python3
from typing import List, NamedTuple, Set, Optional
import sys
from datetime import datetime

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


class BulkMigrationResult(NamedTuple):
    jellyfin_user: Optional[JellyfinUser]
    stats: MigrationStats
    status: str


TranslationLib = List[PathTranslation]
TICKS_PER_MILLISECOND = 10000


def build_translation_library(args: List[str]) -> TranslationLib:
    translations: TranslationLib = []
    for arg in args:
        src, dst = arg.split("|", 1)
        translations.append(PathTranslation(src=src, dst=dst))
    return translations


def build_user_mapping(args: List[str]) -> dict:
    mappings = {}
    for arg in args:
        plex_name, jellyfin_name = arg.split("|", 1)
        mappings[plex_name] = jellyfin_name
    return mappings


def _config_user_maps(raw_user_mappings) -> List[str]:
    if not raw_user_mappings:
        return []
    if isinstance(raw_user_mappings, dict):
        return [f"{plex_name}|{jellyfin_name}" for plex_name, jellyfin_name in raw_user_mappings.items()]
    if isinstance(raw_user_mappings, list):
        return raw_user_mappings
    raise click.BadParameter("user_mappings must be a mapping or list of PLEX|JELLYFIN strings")


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


def _view_offset_ms(item) -> Optional[int]:
    view_offset = getattr(item, "viewOffset", None)
    if not view_offset:
        return None
    try:
        return int(view_offset)
    except (TypeError, ValueError):
        return None


def _item_meta(item) -> dict:
    return {
        "lastViewedAt": getattr(item, "lastViewedAt", None),
        "userRating": getattr(item, "userRating", None),
        "viewOffset": _view_offset_ms(item),
    }


def _remember_plex_item(paths: Set[str], item_meta: dict, item) -> Set[str]:
    parts = _watch_parts(item.media)
    paths.update(parts)
    meta = _item_meta(item)
    for path in parts:
        existing = item_meta.setdefault(path, {})
        for key, value in meta.items():
            if value is not None:
                existing[key] = value
    return parts


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
        "migrate_timestamps": opts.get("migrate_timestamps"),
        "migrate_positions": opts.get("migrate_positions"),
        "secure": opts.get("secure"),
        "translate": raw.get("translations", []) or [],
        "user_map": _config_user_maps(raw.get("user_mappings")),
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
    migrate_timestamps: bool = True,
    migrate_positions: bool = True,
    bulk_mode: bool = False,
) -> MigrationStats:
    stats = MigrationStats()

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
    plex_paths: Set[str] = set()
    plex_watched: Set[str] = set()
    plex_item_meta: dict = {}

    for section in plex.library.sections():
        if isinstance(section, library.MovieSection):
            for m in tqdm(section.search(unwatched=False), desc=f"Movies ({section.title})", unit=" movie", leave=False):
                parts = _remember_plex_item(plex_paths, plex_item_meta, m)
                plex_watched.update(parts)

            if migrate_positions:
                try:
                    in_progress_movies = section.search(inProgress=True)
                except Exception:
                    logger.warning(
                        f"PlexAPI inProgress movie filter not supported for '{section.title}' — "
                        f"falling back to client-side filter"
                    )
                    in_progress_movies = [
                        m for m in section.search() if _view_offset_ms(m)
                    ]

                for m in tqdm(
                    in_progress_movies,
                    desc=f"Movie positions ({section.title})",
                    unit=" movie",
                    leave=False,
                ):
                    _remember_plex_item(plex_paths, plex_item_meta, m)

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
                    parts = _remember_plex_item(plex_paths, plex_item_meta, ep)
                    plex_watched.update(parts)

            if migrate_positions:
                try:
                    in_progress_episodes = section.searchEpisodes(inProgress=True)
                except Exception:
                    logger.warning(
                        f"PlexAPI inProgress episode filter not supported for '{section.title}' — "
                        f"falling back to client-side filter"
                    )
                    in_progress_episodes = [
                        ep for ep in section.searchEpisodes() if _view_offset_ms(ep)
                    ]

                for ep in tqdm(
                    in_progress_episodes,
                    desc=f"Episode positions ({section.title})",
                    unit=" episode",
                    leave=False,
                ):
                    _remember_plex_item(plex_paths, plex_item_meta, ep)

        else:
            logger.info(
                f"Section '{section.title}' ({type(section).__name__}) is not supported — skipped"
            )

    # Match and migrate
    for plex_path in tqdm(plex_paths, desc="Migrating", unit=" item", leave=False):
        meta = plex_item_meta.get(plex_path, {})
        tr_watched = translate_path(plex_path, translations)

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

            if plex_path in plex_watched and not user_data.get("Played"):
                stats.marked += 1
                date_played = None
                if migrate_timestamps:
                    lv = meta.get("lastViewedAt")
                    if isinstance(lv, datetime):
                        date_played = lv.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                if not dry_run:
                    try:
                        jf.mark_watched(user_id=jf_user.id, item_id=item_id, date_played=date_played)
                        logger.bind(path=tr_watched, jf_id=item_id, title=item_name).info("Marked as watched")
                    except JellyfinAPIError as e:
                        logger.error(f"Failed to mark '{item_name}' as watched: {e}")
                else:
                    logger.bind(path=tr_watched, jf_id=item_id, title=item_name).info("Would be marked as watched (dry run)")
            elif plex_path in plex_watched:
                stats.skipped += 1
                logger.bind(path=tr_watched, jf_id=item_id, title=item_name).debug("Already watched — skipped")

            user_rating = meta.get("userRating")
            view_offset_ms = meta.get("viewOffset")

            if migrate_positions and view_offset_ms:
                position_ticks = view_offset_ms * TICKS_PER_MILLISECOND
                stats.playback_positions_set += 1
                if not dry_run:
                    try:
                        jf.set_playback_position(jf_user.id, item_id, position_ticks)
                    except JellyfinAPIError as e:
                        logger.error(f"Failed to set playback position for '{item_name}': {e}")
                else:
                    logger.bind(
                        path=tr_watched,
                        jf_id=item_id,
                        title=item_name,
                        position_ticks=position_ticks,
                    ).info("Would set playback position (dry run)")

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
@click.option("--user-map", type=str, multiple=True, default=[],
              help="Map Plex user to Jellyfin user PLEX|JELLYFIN (repeatable)")
@click.option("--migrate-ratings/--no-migrate-ratings", default=False,
              help="Migrate Plex ratings to Jellyfin")
@click.option("--migrate-favorites/--no-migrate-favorites", default=False,
              help="Migrate highly-rated Plex items (>=9) as Jellyfin favorites")
@click.option("--migrate-timestamps/--no-migrate-timestamps", default=True,
              help="Migrate Plex lastViewedAt to Jellyfin DatePlayed")
@click.option("--migrate-positions/--no-migrate-positions", default=True,
              help="Migrate Plex viewOffset resume positions to Jellyfin")
@click.option("--secure/--insecure", default=False, help="Verify SSL certificates")
@click.option("--debug/--no-debug", default=False, help="Verbose debug logging")
@click.option("--no-skip/--skip", default=False, help="Exit (or fail user) on unmatched paths")
@click.option("--dry-run", is_flag=True, default=False, help="Preview without writing to Jellyfin")
def migrate(plex_url, plex_token, plex_managed_user, jellyfin_url, jellyfin_token,
            jellyfin_user, all_users, auto_create_user, translate, user_map, migrate_ratings,
            migrate_favorites, migrate_timestamps, migrate_positions, secure, debug,
            no_skip, dry_run):
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
    user_mappings = build_user_mapping(list(user_map))

    if all_users:
        plex_users = discover_plex_users(plex, plex_token)
        all_stats: dict = {}

        for plex_user in plex_users:
            logger.info(f"Processing Plex user '{plex_user.name}'...")
            mapped_name = user_mappings.get(plex_user.name)
            jf_user = resolve_jellyfin_user(
                jf,
                plex_user.name,
                auto_create_user,
                dry_run,
                mapped_name=mapped_name,
            )
            if not jf_user:
                status = "Would create" if dry_run and auto_create_user else "Skipped"
                all_stats[plex_user.name] = BulkMigrationResult(
                    jellyfin_user=None,
                    stats=MigrationStats(),
                    status=status,
                )
                continue
            scoped_plex = PlexServer(plex_url, plex_user.token, session=session)
            try:
                stats = migrate_user(scoped_plex, jf, jf_user, translations,
                                     dry_run, no_skip, migrate_ratings, migrate_favorites,
                                     migrate_timestamps, migrate_positions, bulk_mode=True)
                status = "Would migrate" if dry_run else "Migrated"
                all_stats[plex_user.name] = BulkMigrationResult(jf_user, stats, status)
            except Exception as e:
                logger.error(f"Migration failed for '{plex_user.name}': {e}")
                all_stats[plex_user.name] = BulkMigrationResult(
                    jellyfin_user=jf_user,
                    stats=MigrationStats(),
                    status="Failed",
                )

        _print_bulk_summary(all_stats, dry_run)
    else:
        jf_users = jf.get_users()
        jf_user = next((u for u in jf_users if u.name == jellyfin_user), None)
        if not jf_user:
            jf_user = next((u for u in jf_users if u.name.lower() == jellyfin_user.lower()), None)
        if not jf_user:
            raise click.ClickException(f"Jellyfin user '{jellyfin_user}' not found")

        stats = migrate_user(plex, jf, jf_user, translations,
                             dry_run, no_skip, migrate_ratings, migrate_favorites,
                             migrate_timestamps, migrate_positions)
        action = "Would migrate" if dry_run else "Successfully migrated"
        logger.bind(marked=stats.marked, missing=stats.missing, skipped=stats.skipped,
                    ratings=stats.ratings_set, favorites=stats.favorites_set,
                    positions=stats.playback_positions_set).success(
            f"{action} watched states for '{jellyfin_user}'"
        )


def _print_bulk_summary(all_stats: dict, dry_run: bool) -> None:
    action = "Would migrate" if dry_run else "Migration complete"
    print(f"\n{action} — summary:\n")
    header = f"{'User':<32} {'Status':<13} {'Marked':>7} {'Missing':>8} {'Skipped':>8} {'Ratings':>8} {'Favorites':>10} {'Positions':>10}"
    print(header)
    print("-" * len(header))
    for plex_name, result in all_stats.items():
        jf_user = result.jellyfin_user
        stats = result.stats
        label = plex_name
        if jf_user and jf_user.name != plex_name:
            label = f"{plex_name} -> {jf_user.name}"
        print(f"{label:<32} {result.status:<13} {stats.marked:>7} {stats.missing:>8} {stats.skipped:>8} "
              f"{stats.ratings_set:>8} {stats.favorites_set:>10} {stats.playback_positions_set:>10}")
    print()


if __name__ == "__main__":
    migrate()
