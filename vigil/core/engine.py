import asyncio
import logging
import sys
from typing import List
from vigil.core.plugin import BasePlugin
from vigil.core.config import VigilConfig
from vigil.core.database import VigilDatabase
from vigil.core.collectors.ssh_collector import SSHCollector
from vigil.core.controllers.ssh_controller import SSHController
from peewee import OperationalError

class VigilEngine:
    def __init__(self, config_path):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[BasePlugin] = []
        try:
            self.db = VigilDatabase(self.config_loader.database_settings.get('path', 'vigil.db'))
            self.db.insert_event("INFO", "Vigil Engine initialized.", "vigil_core")
        except OperationalError as e:
            logging.critical(f"Failed to initialize database: {e}. Exiting.")
            sys.exit(1)

    def setup_modules(self):
        """
        Dynamically loads domain-based plugins based on the configuration.
        """
        logging.info("Initializing plugins by domain...")
        # Placeholder for dynamic loading logic
        # for plugin_cfg in self.config_loader.plugins:
        #     self.plugins.append(load_domain_plugin(plugin_cfg))
        pass

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

            await asyncio.sleep(60)

def main():
    logging.basicConfig(level=logging.INFO)
    # Example entry point
    # engine = VigilEngine("config.yaml")
    # asyncio.run(engine.run())
    print("Vigil Engine Scaffolding Loaded.")

if __name__ == "__main__":
    main()