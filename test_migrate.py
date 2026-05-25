from datetime import datetime

import pytest
from unittest.mock import MagicMock, patch, call
from plexapi import library as plex_library

from models import JellyfinUser, MigrationStats
from jellyfin_client import JellyFinServer
from migrate import migrate_user, PathTranslation, build_translation_library, translate_path


def make_plex_movie(file_path: str, user_rating=None, last_viewed_at=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    movie = MagicMock()
    movie.media = [medium]
    movie.userRating = user_rating
    movie.lastViewedAt = last_viewed_at
    return movie


def make_plex_episode(file_path: str, user_rating=None, last_viewed_at=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    ep = MagicMock()
    ep.media = [medium]
    ep.userRating = user_rating
    ep.lastViewedAt = last_viewed_at
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

        jf.mark_watched.assert_called_once_with(user_id="jf_uid", item_id="jf1", date_played=None)
        assert stats.marked == 1

    def test_passes_last_viewed_at_timestamp(self, jf, jf_user):
        viewed_at = datetime(2023, 10, 15, 14, 30, 0)
        movie = make_plex_movie("/media/film.mkv", last_viewed_at=viewed_at)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([movie])])

        migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                     migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_called_once_with(
            user_id="jf_uid", item_id="jf1", date_played="2023-10-15T14:30:00.000000Z"
        )

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
