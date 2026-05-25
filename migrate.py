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
        "migrate_timestamps": opts.get("migrate_timestamps"),
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
    migrate_timestamps: bool = True,
    bulk_mode: bool = False,
) -> MigrationStats:
    stats = MigrationStats()
    track_item_meta = migrate_ratings or migrate_favorites or migrate_timestamps

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
                meta = {"lastViewedAt": getattr(m, "lastViewedAt", None)}
                if track_item_meta:
                    meta["userRating"] = getattr(m, "userRating", None)
                for p in parts:
                    plex_item_meta[p] = meta

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
                    meta = {"lastViewedAt": getattr(ep, "lastViewedAt", None)}
                    if track_item_meta:
                        meta["userRating"] = getattr(ep, "userRating", None)
                    for p in parts:
                        plex_item_meta[p] = meta

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
              help="Migrate highly-rated Plex items (>=9) as Jellyfin favorites")
@click.option("--migrate-timestamps/--no-migrate-timestamps", default=True,
              help="Migrate Plex lastViewedAt to Jellyfin DatePlayed")
@click.option("--secure/--insecure", default=False, help="Verify SSL certificates")
@click.option("--debug/--no-debug", default=False, help="Verbose debug logging")
@click.option("--no-skip/--skip", default=False, help="Exit (or fail user) on unmatched paths")
@click.option("--dry-run", is_flag=True, default=False, help="Preview without writing to Jellyfin")
def migrate(plex_url, plex_token, plex_managed_user, jellyfin_url, jellyfin_token,
            jellyfin_user, all_users, auto_create_user, translate, migrate_ratings,
            migrate_favorites, migrate_timestamps, secure, debug, no_skip, dry_run):
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
                                     migrate_timestamps, bulk_mode=True)
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
                             dry_run, no_skip, migrate_ratings, migrate_favorites,
                             migrate_timestamps)
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
            label = f"{plex_name} -> {jf_user.name}"
        print(f"{label:<20} {stats.marked:>7} {stats.missing:>8} {stats.skipped:>8} "
              f"{stats.ratings_set:>8} {stats.favorites_set:>10}")
    print()


if __name__ == "__main__":
    migrate()
