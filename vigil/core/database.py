import sqlite3
import logging
from pathlib import Path

class VigilDatabase:
    """
    A lightweight SQLite database manager for storing settings and metrics.
    Uses the standard library sqlite3 for zero-dependency storage.
    """
    def __init__(self, db_path: str = "vigil.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Initializes the database schema if it doesn't exist."""
        schema = """
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            target TEXT,
            collector TEXT,
            metric_name TEXT,
            value REAL,
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            level TEXT,
            message TEXT,
            target TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- Performance indexes for time-series retrieval
        CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics (target, metric_name, timestamp);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
        """
        try:
            with self._get_connection() as conn:
                conn.executescript(schema)
            logging.info(f"Database initialized at {self.db_path}")
        except Exception as e:
            logging.error(f"Failed to initialize database: {e}")
            raise

    def execute(self, query: str, params: tuple = ()):
        """Helper to execute a query and return results."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()