import argparse
import logging
import sys
from vigil.core.main import VigilEngine
from vigil.core.ui.main_dashboard import init_gui

def main():
    parser = argparse.ArgumentParser(description="Vigil Monitoring System")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--db", help="Path to the SQLite database file (overrides config)")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web dashboard / GUI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    
    engine = VigilEngine(args.config, db_path_override=args.db)
    
    init_gui(db_path=engine.db_path, port=args.port, engine_run_func=engine.run)

if __name__ == "__main__":
    main()