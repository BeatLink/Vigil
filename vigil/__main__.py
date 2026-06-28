import argparse
import logging
import sys
from vigil.core.main import VigilEngine

def main():
    parser = argparse.ArgumentParser(description="Vigil Monitoring System")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--db", help="Path to the SQLite database file (overrides config)")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web dashboard / GUI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    engine = VigilEngine(args.config, db_path_override=args.db)

    # Apply theme overrides before any UI module is imported so that
    # 'from .theme import PRIMARY' bindings in main_dashboard/components
    # and lazily-loaded plugins all see the configured values.
    import vigil.core.ui.theme as theme
    theme.configure(engine.config_loader.theme_settings)

    from vigil.core.ui.main_dashboard import init_gui
    engine.setup_modules()

    init_gui(engine=engine, port=args.port)

if __name__ == "__main__":
    main()
