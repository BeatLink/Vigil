import argparse
import asyncio
import logging

from vigil.core.connectors.engine import VigilEngine


def main():
    parser = argparse.ArgumentParser(description="Vigil Collector")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--db", help="Path to the SQLite database file (overrides config)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    engine = VigilEngine(args.config, db_path_override=args.db)
    engine.setup_modules()

    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
