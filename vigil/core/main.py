import asyncio
import logging
import importlib
import inspect
import sys
from typing import List, Optional, Dict
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.config_file import ConfigFileManager as VigilConfig
from vigil.core.data.database import DatabaseManager as VigilDatabase
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

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[BasePlugin]:
        """
        Dynamically instantiates plugins and injects internal modules.
        Supports recursive loading for nested group structures.
        """
        current_level_plugins = []
        target_cfg = plugins_cfg if plugins_cfg is not None else self.config_loader.plugins

        for plugin_cfg in target_cfg:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            # Dynamically load the plugin class and inject dependencies
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)
                
                # Find class inheriting from BasePlugin
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BasePlugin) and obj is not BasePlugin:
                        plugin_instance = obj(name, plugin_cfg, self.db)
                        
                        # Recursively setup children if they exist
                        if 'children' in plugin_cfg:
                            plugin_instance.children = self.setup_modules(plugin_cfg['children'])
                        
                        current_level_plugins.append(plugin_instance)
                        logging.info(f"Loaded plugin '{name}' of type '{p_type}'")
                        break
            except Exception as e:
                logging.error(f"Failed to load plugin '{name}' ({p_type}): {e}")
        
        # If this is the root call, store the root-level plugins in the engine
        if plugins_cfg is None:
            self.plugins = current_level_plugins
            logging.info(f"Plugin registry built with {len(self.plugins)} root-level monitors.")

        return current_level_plugins

    async def run(self):
        logging.info("Vigil Engine started...")
        self.db.insert_event("INFO", "Vigil Engine started polling loop.", "vigil_core")
        
        while True:
            # Recursively collect all plugins to be polled
            all_pollable = []
            
            def collect_recursive(plugins):
                for p in plugins:
                    # All plugins (including groups) should have their on_collect called
                    # GroupPlugin's on_collect will perform aggregation
                    all_pollable.append(p) 
                    collect_recursive(p.children)
            
            collect_recursive(self.plugins)
            tasks = [p.run_cycle() for p in all_pollable]
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                # Handle potential exceptions from gathered tasks
                for res in results:
                    if isinstance(res, Exception):
                        logging.error(f"Plugin execution error: {res}")
                logging.info(f"Engine Cycle: Processed {len(results)} monitors.")
            else:
                logging.debug("No plugins configured.")
            
            await asyncio.sleep(60)