import argparse
import logging

from vigil.core.ui.engine import VigilWebEngine


def main():
    parser = argparse.ArgumentParser(description="Vigil Web Dashboard")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--db", help="Path to the SQLite database file (overrides config)")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web dashboard / GUI")
    parser.add_argument("--collector-url", default=None,
                        help="Base URL of the collector process's internal API. "
                             "Defaults to config.yaml's internal_api.host/port "
                             "(same section the collector reads to know where to "
                             "bind), so a deployment only sets that once.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.collector_url:
        collector_url = args.collector_url
    else:
        from vigil.core.database.config_file import ConfigFileManager
        from vigil.core.connectors.engine import DEFAULT_INTERNAL_API_HOST, DEFAULT_INTERNAL_API_PORT
        api_cfg = ConfigFileManager(args.config).data.get('internal_api', {}) or {}
        host = api_cfg.get('host', DEFAULT_INTERNAL_API_HOST)
        port = api_cfg.get('port', DEFAULT_INTERNAL_API_PORT)
        collector_url = f'http://{host}:{port}'

    engine = VigilWebEngine(args.config, db_path_override=args.db, collector_url=collector_url)

    import vigil.core.ui.ui.theme as theme
    theme.configure(engine.config_loader.theme_settings)

    from vigil.core.ui.ui.main_dashboard import init_gui
    engine.setup_ui_modules()

    init_gui(engine=engine, port=args.port)


if __name__ == "__main__":
    main()
