from datetime import datetime

import pytest
from unittest.mock import MagicMock, patch, call
from plexapi import library as plex_library

from models import PlexUser, JellyfinUser, MigrationStats
from jellyfin_client import JellyFinServer
from migrate import (
    migrate,
    migrate_user,
    PathTranslation,
    build_translation_library,
    build_user_mapping,
    translate_path,
)


def make_plex_movie(file_path: str, user_rating=None, last_viewed_at=None, view_offset=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    movie = MagicMock()
    movie.media = [medium]
    movie.userRating = user_rating
    movie.lastViewedAt = last_viewed_at
    movie.viewOffset = view_offset
    return movie


def make_plex_episode(file_path: str, user_rating=None, last_viewed_at=None, view_offset=None):
    part = MagicMock()
    part.file = file_path
    medium = MagicMock()
    medium.parts = [part]
    ep = MagicMock()
    ep.media = [medium]
    ep.userRating = user_rating
    ep.lastViewedAt = last_viewed_at
    ep.viewOffset = view_offset
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


def make_movie_section(movies, in_progress_movies=None):
    section = MagicMock(spec=plex_library.MovieSection)
    section.title = "Movies"
    in_progress_movies = [] if in_progress_movies is None else in_progress_movies

    def search(**kwargs):
        if kwargs.get("inProgress"):
            return in_progress_movies
        return movies

    section.search.side_effect = search
    return section


def make_show_section(shows, in_progress_episodes=None):
    section = MagicMock(spec=plex_library.ShowSection)
    section.title = "TV Shows"
    section.searchShows.return_value = shows
    section.searchEpisodes.return_value = [] if in_progress_episodes is None else in_progress_episodes
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


class TestMigrateUserPlaybackPositions:
    def test_migrates_resume_position_for_in_progress_movie(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", view_offset=12345)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([], in_progress_movies=[movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_not_called()
        jf.set_playback_position.assert_called_once_with("jf_uid", "jf1", 123450000)
        assert stats.playback_positions_set == 1

    def test_migrates_resume_position_for_in_progress_episode(self, jf, jf_user):
        episode = make_plex_episode("/media/show/s01e01.mkv", view_offset=7654)
        jf_item = make_jf_item("jf_ep1", "/media/show/s01e01.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_show_section([], in_progress_episodes=[episode])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False)

        jf.mark_watched.assert_not_called()
        jf.set_playback_position.assert_called_once_with("jf_uid", "jf_ep1", 76540000)
        assert stats.playback_positions_set == 1

    def test_does_not_migrate_resume_position_when_disabled(self, jf, jf_user):
        movie = make_plex_movie("/media/film.mkv", view_offset=12345)
        jf_item = make_jf_item("jf1", "/media/film.mkv", played=False)
        jf.iter_items.return_value = iter([jf_item])
        plex = make_plex([make_movie_section([], in_progress_movies=[movie])])

        stats = migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                             migrate_ratings=False, migrate_favorites=False,
                             migrate_positions=False)

        jf.set_playback_position.assert_not_called()
        assert stats.playback_positions_set == 0

    def test_uses_matching_item_timestamp_for_each_path(self, jf, jf_user):
        first_viewed_at = datetime(2023, 10, 15, 14, 30, 0)
        second_viewed_at = datetime(2024, 1, 2, 3, 4, 5)
        first_movie = make_plex_movie("/media/first.mkv", last_viewed_at=first_viewed_at)
        second_movie = make_plex_movie("/media/second.mkv", last_viewed_at=second_viewed_at)
        jf.iter_items.return_value = iter([
            make_jf_item("jf1", "/media/first.mkv", played=False),
            make_jf_item("jf2", "/media/second.mkv", played=False),
        ])
        plex = make_plex([make_movie_section([first_movie, second_movie])])

        migrate_user(plex, jf, jf_user, [], dry_run=False, no_skip=False,
                     migrate_ratings=False, migrate_favorites=False)

        assert call(user_id="jf_uid", item_id="jf1", date_played="2023-10-15T14:30:00.000000Z") in jf.mark_watched.call_args_list
        assert call(user_id="jf_uid", item_id="jf2", date_played="2024-01-02T03:04:05.000000Z") in jf.mark_watched.call_args_list


class TestCliOptions:
    def test_help_lists_migrate_positions_option(self):
        from click.testing import CliRunner

        result = CliRunner().invoke(migrate, ["--help"])

        assert "--migrate-positions / --no-migrate-positions" in result.output

    def test_builds_user_mapping_from_cli_values(self):
        assert build_user_mapping(["Plex User|Jellyfin User"]) == {
            "Plex User": "Jellyfin User"
        }

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.discover_plex_users")
    def test_all_users_dry_run_summary_includes_users_that_would_be_created(
        self,
        discover_plex_users,
        jellyfin_server,
        plex_server,
    ):
        from click.testing import CliRunner

        discover_plex_users.return_value = [
            PlexUser(name="Carol", token="carol_token", is_managed=True)
        ]
        jellyfin_server.return_value.get_users.return_value = []

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--all-users",
            "--dry-run",
        ])

        assert result.exit_code == 0
        assert "Status" in result.output
        assert "Carol" in result.output
        assert "Would create" in result.output

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.discover_plex_users")
    @patch("migrate.migrate_user")
    def test_all_users_uses_configured_user_mapping(
        self,
        migrate_user_mock,
        discover_plex_users,
        jellyfin_server,
        plex_server,
        tmp_path,
    ):
        from click.testing import CliRunner
        import yaml

        config_path = tmp_path / "config.yml"
        config_path.write_text(yaml.dump({
            "plex": {"url": "http://plex.local", "token": "plex_token"},
            "jellyfin": {"url": "http://jellyfin.local", "token": "jellyfin_token"},
            "options": {"all_users": True, "dry_run": True},
            "user_mappings": {"Carol Plex": "Bob"},
        }))
        discover_plex_users.return_value = [
            PlexUser(name="Carol Plex", token="carol_token", is_managed=True)
        ]
        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="2", name="Bob")
        ]
        migrate_user_mock.return_value = MigrationStats(marked=3)

        result = CliRunner().invoke(migrate, ["--config", str(config_path)])

        assert result.exit_code == 0
        assert "Carol Plex -> Bob" in result.output
        assert "Would migrate" in result.output
        migrate_user_mock.assert_called_once()
