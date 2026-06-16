import asyncio
import logging
import importlib
import inspect
import sys
from typing import List, Optional
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.config_file import ConfigFileManager as VigilConfig
from vigil.core.data.database import DatabaseManager as VigilDatabase
# Note: The following modules are referenced but not present in the current context
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.modules.collectors.ssh_collector import SSHCollector
from vigil.core.modules.controllers.ssh_controller import SSHController
from peewee import OperationalError

class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[BasePlugin] = []
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            self.db = VigilDatabase(self.db_path)
            self.db.insert_event("INFO", "Vigil Engine initialized.", "vigil_core")
        except OperationalError as e:
            logging.critical(f"Failed to initialize database: {e}. Exiting.")
            sys.exit(1)

    def setup_modules(self):
        """
        Dynamically instantiates plugins and injects internal modules.
        """
        logging.info("Building plugin registry and injecting dependencies...")
        
        for plugin_cfg in self.config_loader.plugins:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            ssh_cfg = plugin_cfg.get('ssh_config', {})
            target = plugin_cfg.get('target_host', ssh_cfg.get('host', 'localhost'))

            # 1. Initialize shared SSH infrastructure for this plugin
            ssh_conn = SSHConnection(
                host=ssh_cfg.get('host', target),
                username=ssh_cfg.get('username'),
                key_path=ssh_cfg.get('key_path'),
                password=ssh_cfg.get('password'),
                port=ssh_cfg.get('port')
            )

            # 2. Prepare the internal modules registry
            internal = {
                'collectors': {'ssh': SSHCollector(ssh_conn)},
                'controllers': {'ssh': SSHController(ssh_conn)},
                'loggers': {
                    'db_logs': self.db.get_logger(target, name),
                    'db_metrics': self.db.get_logger(target, name)
                }
            }

            # 3. Dynamically load the plugin class
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)
                
                # Find class inheriting from BasePlugin
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BasePlugin) and obj is not BasePlugin:
                        plugin_instance = obj(name, plugin_cfg, internal)
                        self.plugins.append(plugin_instance)
                        logging.info(f"Loaded plugin '{name}' of type '{p_type}'")
                        break
            except Exception as e:
                logging.error(f"Failed to load plugin '{name}' ({p_type}): {e}")

    async def run(self):
        logging.info("Vigil Engine started...")
        self.db.insert_event("INFO", "Vigil Engine started polling loop.", "vigil_core")
        self.setup_modules()
        
        while True:
            tasks = []
            for plugin in self.plugins:
                tasks.append(plugin.run_cycle())
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                # Handle potential exceptions from gathered tasks
                for res in results:
                    if isinstance(res, Exception):
                        logging.error(f"Plugin execution error: {res}")
                logging.info(f"Processed {len(results)} collection tasks.")
            else:
                logging.debug("No plugins configured.")