import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

class VigilConfig:
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
    def collectors(self) -> List[Dict[str, Any]]:
        """Returns the list of collector configurations."""
        return self.data.get('collectors', [])

    @property
    def alert_handlers(self) -> List[Dict[str, Any]]:
        """Returns the list of alerting configurations."""
        return self.data.get('alerting', [])

    @property
    def controllers(self) -> List[Dict[str, Any]]:
        """Returns the list of control/remediation configurations."""
        return self.data.get('control', [])