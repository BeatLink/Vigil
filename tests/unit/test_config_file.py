import pytest
import yaml
from vigil.core.data.config_file import ConfigFileManager


@pytest.fixture
def write_yaml(tmp_path):
    """Helper: writes a dict as YAML to a temp file and returns the path."""
    def _write(content: dict) -> str:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(content))
        return str(path)
    return _write


class TestConfigFileLoading:
    def test_loads_valid_config(self, write_yaml):
        path = write_yaml({"database": {"path": "my.db"}, "plugins": []})
        cfg = ConfigFileManager(path)
        assert cfg.data["database"]["path"] == "my.db"

    def test_missing_file_returns_empty_data(self, tmp_path):
        cfg = ConfigFileManager(str(tmp_path / "does_not_exist.yaml"))
        assert cfg.data == {}

    def test_malformed_yaml_returns_empty_data(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("key: [unclosed bracket")
        cfg = ConfigFileManager(str(path))
        assert cfg.data == {}

    def test_non_dict_yaml_returns_empty(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n")
        cfg = ConfigFileManager(str(path))
        assert cfg.data == {}


class TestDatabaseSettings:
    def test_returns_configured_path(self, write_yaml):
        path = write_yaml({"database": {"path": "/data/vigil.db"}})
        cfg = ConfigFileManager(path)
        assert cfg.database_settings["path"] == "/data/vigil.db"

    def test_defaults_to_vigil_db_when_missing(self, write_yaml):
        path = write_yaml({"plugins": []})
        cfg = ConfigFileManager(path)
        assert cfg.database_settings["path"] == "vigil.db"

    def test_defaults_when_file_missing(self, tmp_path):
        cfg = ConfigFileManager(str(tmp_path / "missing.yaml"))
        assert cfg.database_settings["path"] == "vigil.db"


class TestPluginsProperty:
    def test_returns_plugin_list(self, write_yaml):
        path = write_yaml({"plugins": [
            {"name": "A", "type": "uptime"},
            {"name": "B", "type": "group"},
        ]})
        cfg = ConfigFileManager(path)
        assert len(cfg.plugins) == 2
        assert cfg.plugins[0]["name"] == "A"

    def test_empty_when_key_absent(self, write_yaml):
        path = write_yaml({"database": {"path": "x.db"}})
        cfg = ConfigFileManager(path)
        assert cfg.plugins == []

    def test_empty_when_file_missing(self, tmp_path):
        cfg = ConfigFileManager(str(tmp_path / "missing.yaml"))
        assert cfg.plugins == []


class TestSSHDefaults:
    def test_returns_configured_defaults(self, write_yaml):
        path = write_yaml({"ssh_defaults": {"username": "beatlink", "key_path": "/run/vigil.key"}})
        cfg = ConfigFileManager(path)
        assert cfg.ssh_defaults == {"username": "beatlink", "key_path": "/run/vigil.key"}

    def test_empty_when_missing(self, write_yaml):
        path = write_yaml({"plugins": []})
        cfg = ConfigFileManager(path)
        assert cfg.ssh_defaults == {}

    def test_empty_when_file_missing(self, tmp_path):
        cfg = ConfigFileManager(str(tmp_path / "missing.yaml"))
        assert cfg.ssh_defaults == {}


class TestAlertAndControlProperties:
    def test_alert_handlers_empty_when_missing(self, write_yaml):
        path = write_yaml({"plugins": []})
        cfg = ConfigFileManager(path)
        assert cfg.alert_handlers == []

    def test_controllers_empty_when_missing(self, write_yaml):
        path = write_yaml({"plugins": []})
        cfg = ConfigFileManager(path)
        assert cfg.controllers == []
