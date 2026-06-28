import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

class ConfigFileManager:
    """
    Handles loading and processing of the Vigil YAML configuration file.
    """
    def __init__(self, config_path: str):
        self.path = Path(config_path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        """Loads YAML content from the config path with basic error handling."""
        if not self.path.exists():
            logging.warning(f"Configuration file not found at {self.path}. Using empty configuration defaults.")
            return {}
        
        try:
            with open(self.path, 'r') as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except yaml.YAMLError as e:
            logging.error(f"Failed to parse YAML configuration at {self.path}: {e}")
            return {}

    @property
    def database_settings(self) -> Dict[str, Any]:
        """Returns the database section or default path."""
        return self.data.get('database', {'path': 'vigil.db'})

    @property
    def plugins(self) -> List[Dict[str, Any]]:
        """Returns the list of plugin configurations."""
        return self.data.get('plugins', [])

    @property
    def alert_handlers(self) -> List[Dict[str, Any]]:
        """Returns the list of alerting configurations."""
        return self.data.get('alerting', [])

    @property
    def theme_settings(self) -> Dict[str, Any]:
        """Returns the theme overrides section (may be empty)."""
        return self.data.get('theme', {})

    @property
    def controllers(self) -> List[Dict[str, Any]]:
        """Returns the list of control/remediation configurations."""
        return self.data.get('control', [])