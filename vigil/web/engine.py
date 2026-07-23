"""
Web-process counterpart to VigilEngine (core/main.py).

VigilEngine is the collector: it loads config, builds CollectorPlugin
instances with real SSH connections, and runs the per-monitor scheduler.
VigilWebEngine is deliberately much smaller — it loads the same config, opens
the same shared SQLite database for reads, and builds UIPlugin instances
(id/name/render_ui only, no SSH) that proxy actions to the collector's
internal API over CollectorClient. It has no run() and no scheduler: the web
process only serves the dashboard.

Kept as a separate class rather than a mode flag on VigilEngine so the two
roles can't accidentally blend — the same reasoning as splitting
CollectorPlugin/UIPlugin in plugin_base.py.
"""
import importlib
import inspect
import logging
from typing import Any, Dict, List, Optional

from vigil.web.plugin_base import UIPlugin
from vigil.core.data.config_file import ConfigFileManager as VigilConfig
from vigil.core.data.database import DatabaseManager as VigilDatabase
from vigil.web.remote_proxy import CollectorClient
from peewee import OperationalError


class VigilWebEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None,
                 collector_url: str = 'http://127.0.0.1:8081'):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[UIPlugin] = []
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            # Opens the same SQLite file the collector writes to. WAL mode
            # (see DatabaseManager._connect_and_init) is what makes this
            # process's reads and the collector's writes safe concurrently;
            # this process's own writer thread only ever handles UI
            # preferences (drawer width, tree-expanded state — see
            # main_dashboard.py), never monitor data.
            self.db = VigilDatabase(self.db_path, write_batch_seconds=self.config_loader.write_batch_seconds)
        except OperationalError as e:
            logging.critical(f"Failed to open database: {e}. Exiting.")
            raise

        self.collector_client = CollectorClient(base_url=collector_url)

    def setup_ui_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[UIPlugin]:
        """
        Build the read-side plugin tree: one *UIPlugin instance per
        configured monitor, holding no SSH connection.

        Mirrors VigilEngine.setup_modules structurally (same recursive
        children handling, same dynamic module loading by `type`), but
        matches each plugin module's UIPlugin subclass instead of its
        CollectorPlugin subclass, and passes collector_client through so
        on_action/ssh_controller/job_controller can reach the collector.
        """
        current_level_plugins = []
        target_cfg = plugins_cfg if plugins_cfg is not None else self.config_loader.plugins

        for plugin_cfg in target_cfg:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)

                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, UIPlugin) and obj is not UIPlugin:
                        plugin_instance = obj(name, plugin_cfg, self.db, self.collector_client)

                        if 'children' in plugin_cfg:
                            plugin_instance.children = self.setup_ui_modules(plugin_cfg['children'])

                        current_level_plugins.append(plugin_instance)
                        logging.info(f"Loaded UI plugin '{name}' of type '{p_type}'")
                        break
            except Exception as e:
                logging.error(f"Failed to load UI plugin '{name}' ({p_type}): {e}")

        if plugins_cfg is None:
            self.plugins = current_level_plugins
            logging.info(f"UI plugin registry built with {len(self.plugins)} root-level monitors.")

        return current_level_plugins
