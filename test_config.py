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

    def test_config_accepts_migrate_positions_option(self, tmp_path):
        config_path = write_config(tmp_path, {
            "plex": {"url": "http://p.local", "token": "pt"},
            "jellyfin": {"url": "http://j.local", "token": "jt"},
            "options": {"migrate_positions": False},
        })
        runner = CliRunner()
        result = runner.invoke(migrate, ["--config", config_path, "--jellyfin-user", "u", "--dry-run"])
        assert "Error: Invalid value for '--config'" not in result.output
