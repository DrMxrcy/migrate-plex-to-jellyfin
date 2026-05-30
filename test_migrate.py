from datetime import datetime
import json

import pytest
from unittest.mock import MagicMock, patch, call
from plexapi import library as plex_library

from models import PlexUser, JellyfinUser, MigrationStats
from jellyfin_client import JellyFinServer
from migrate import (
    migrate,
    migrate_user,
    PathTranslation,
    build_user_plan,
    build_translation_library,
    build_user_mapping,
    utc_now,
    format_utc,
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

    def test_utc_now_is_timezone_aware_and_formats_with_z_suffix(self):
        now = utc_now()

        assert now.tzinfo is not None
        assert now.utcoffset().total_seconds() == 0
        assert format_utc(now).endswith("Z")

    def test_builds_user_plan_with_matches_mappings_and_create_actions(self):
        rows = build_user_plan(
            plex_users=[
                PlexUser(name="JP", token="jp_token", is_managed=False),
                PlexUser(name="Mxrcy", token="mxrcy_token", is_managed=True),
                PlexUser(name="Gavin Snell (Gavin8tor245)", token="gavin_token", is_managed=True),
                PlexUser(name="Bad Map", token="bad_token", is_managed=True),
            ],
            jellyfin_users=[
                JellyfinUser(id="1", name="john"),
                JellyfinUser(id="2", name="Mxrcy"),
            ],
            user_mappings={
                "JP": "john",
                "Bad Map": "missing-user",
            },
            auto_create=True,
        )

        by_name = {row.plex_name: row for row in rows}
        assert by_name["JP"].target_name == "john"
        assert by_name["JP"].status == "Mapped"
        assert by_name["Mxrcy"].status == "Exact match"
        assert by_name["Gavin Snell (Gavin8tor245)"].status == "Would create"
        assert by_name["Bad Map"].status == "Mapping missing"

    def test_builds_user_plan_skip_when_auto_create_disabled(self):
        rows = build_user_plan(
            plex_users=[PlexUser(name="No Match", token="token", is_managed=True)],
            jellyfin_users=[],
            user_mappings={},
            auto_create=False,
        )

        assert rows[0].status == "Skipped"

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.discover_plex_users")
    @patch("migrate.migrate_user")
    def test_plan_users_prints_plan_without_migrating(
        self,
        migrate_user_mock,
        discover_plex_users,
        jellyfin_server,
        plex_server,
    ):
        from click.testing import CliRunner

        discover_plex_users.return_value = [
            PlexUser(name="JP", token="jp_token", is_managed=False),
            PlexUser(name="Gavin Snell (Gavin8tor245)", token="gavin_token", is_managed=True),
        ]
        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="1", name="john")
        ]

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--all-users",
            "--plan-users",
            "--user-map", "JP|john",
        ])

        assert result.exit_code == 0
        assert "User plan" in result.output
        assert "JP -> john" in result.output
        assert "Mapped" in result.output
        assert "Gavin Snell (Gavin8tor245)" in result.output
        assert "Would create" in result.output
        assert "user_mappings:" in result.output
        migrate_user_mock.assert_not_called()

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    def test_passes_configured_plex_timeout_to_plex_server(
        self,
        jellyfin_server,
        plex_server,
    ):
        from click.testing import CliRunner

        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="1", name="john")
        ]

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--jellyfin-user", "john",
            "--plex-timeout", "120",
            "--dry-run",
        ])

        assert result.exit_code == 0
        assert plex_server.call_args.kwargs["timeout"] == 120

    @patch("migrate.PlexServer")
    def test_plex_connection_timeout_reports_clean_error(self, plex_server):
        from click.testing import CliRunner
        import requests

        plex_server.side_effect = requests.exceptions.ReadTimeout("timed out")

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--jellyfin-user", "john",
            "--plex-timeout", "120",
        ])

        assert result.exit_code != 0
        assert "Could not connect to Plex" in result.output
        assert "http://plex.local" in result.output
        assert "120s" in result.output

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

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.migrate_user")
    def test_single_user_dry_run_writes_json_and_text_reports(
        self,
        migrate_user_mock,
        jellyfin_server,
        plex_server,
        tmp_path,
    ):
        from click.testing import CliRunner

        report_dir = tmp_path / "reports"
        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="1", name="john")
        ]
        migrate_user_mock.return_value = MigrationStats(
            marked=2,
            missing=1,
            skipped=3,
            ratings_set=4,
            favorites_set=5,
            playback_positions_set=6,
        )

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--jellyfin-user", "john",
            "--dry-run",
            "--report-dir", str(report_dir),
        ])

        assert result.exit_code == 0
        json_reports = list(report_dir.glob("*.json"))
        text_reports = list(report_dir.glob("*.txt"))
        assert len(json_reports) == 1
        assert len(text_reports) == 1
        data = json.loads(json_reports[0].read_text())
        assert data["dry_run"] is True
        assert data["users"][0]["plex_name"] == "john"
        assert data["users"][0]["stats"]["marked"] == 2
        assert "Report written:" in result.output

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.migrate_user")
    def test_report_write_failure_warns_without_failing_migration(
        self,
        migrate_user_mock,
        jellyfin_server,
        plex_server,
        tmp_path,
    ):
        from click.testing import CliRunner

        report_dir_file = tmp_path / "not-a-directory"
        report_dir_file.write_text("blocking file")
        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="1", name="john")
        ]
        migrate_user_mock.return_value = MigrationStats(marked=1)

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--jellyfin-user", "john",
            "--dry-run",
            "--report-dir", str(report_dir_file),
        ])

        assert result.exit_code == 0
        assert "Failed to write report" in result.output

    @patch("migrate.PlexServer")
    @patch("migrate.JellyFinServer")
    @patch("migrate.discover_plex_users")
    @patch("migrate.build_jellyfin_index")
    @patch("migrate.migrate_user")
    def test_all_users_reuses_one_jellyfin_index(
        self,
        migrate_user_mock,
        build_jellyfin_index,
        discover_plex_users,
        jellyfin_server,
        plex_server,
        tmp_path,
    ):
        from click.testing import CliRunner

        shared_index = {"/media/film.mkv": [make_jf_item("jf1", "/media/film.mkv")]}
        build_jellyfin_index.return_value = shared_index
        discover_plex_users.return_value = [
            PlexUser(name="Alice", token="alice_token", is_managed=False),
            PlexUser(name="Bob", token="bob_token", is_managed=True),
        ]
        jellyfin_server.return_value.get_users.return_value = [
            JellyfinUser(id="1", name="Alice"),
            JellyfinUser(id="2", name="Bob"),
        ]
        migrate_user_mock.return_value = MigrationStats(marked=1)

        result = CliRunner().invoke(migrate, [
            "--plex-url", "http://plex.local",
            "--plex-token", "plex_token",
            "--jellyfin-url", "http://jellyfin.local",
            "--jellyfin-token", "jellyfin_token",
            "--all-users",
            "--dry-run",
            "--report-dir", str(tmp_path / "reports"),
        ])

        assert result.exit_code == 0
        build_jellyfin_index.assert_called_once_with(jellyfin_server.return_value, "1")
        assert migrate_user_mock.call_count == 2
        assert all(call.kwargs["jf_entries"] is shared_index for call in migrate_user_mock.call_args_list)
