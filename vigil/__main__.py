"""Vigil entry point.

Builds the Coordination Engine (``VigilEngine``), loads plugins, configures
the theme, and hands off to the UI Engine (``init_gui``), which registers the
engine's polling loop as a NiceGUI startup hook so collection and the web
dashboard share one asyncio event loop.

``VigilEngine`` lives in ``vigil.core.coordination.engine``; it is re-exported
here so ``vigil.__main__.VigilEngine`` remains a stable import/patch target.
"""

import argparse
import logging

from vigil.core.coordination.engine import VigilEngine

__all__ = ["VigilEngine", "main"]


def main():
    parser = argparse.ArgumentParser(description="Vigil")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--db", help="Path to the SQLite database file (overrides config)")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web dashboard / GUI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    engine = VigilEngine(args.config, db_path_override=args.db)
    engine.setup_modules()

    import vigil.core.ui.theme as theme
    theme.configure(engine.config_loader.theme_settings)

    from vigil.core.ui.main_dashboard import init_gui
    init_gui(engine=engine, port=args.port)


if __name__ == "__main__":
    main()
