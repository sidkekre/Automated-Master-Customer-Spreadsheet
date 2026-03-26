import sqlite3
from datetime import datetime, timedelta

from src.logger import *

class DB_WITH_TTL:
    def __init__(self, db_path, db_name, ttl_days):
        self.db_file = db_path + db_name + '.db'
        self.db_name = db_name
        self.ttl = timedelta(days=ttl_days)
        self._initialize_db()

    @staticmethod
    def _sql_ident(name: str) -> str:
        """
        Wrap a trusted SQLite *identifier* (table/column name).
        Doubles embedded double-quotes per SQL rules so the name is safe inside double quotes.
        """
        return '"' + name.replace('"', '""') + '"'

    def _get_connection(self):
        return sqlite3.connect(self.db_file)

    def _initialize_db(self):
        with self._get_connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._sql_ident(self.db_name)} (
                    envelope_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP
                )
                """
            )
            conn.commit()

    def _purge_expired(self, cursor):
        cutoff = (datetime.now() - self.ttl).isoformat()
        table = self._sql_ident(self.db_name)
        try:
            cursor.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
        except sqlite3.OperationalError as e:
            WarnLogger(f"TTL purge skipped for ({self.db_name!r}): {e}", flush=True)

    def execute_query(self, query, params=(), fetch=False):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._purge_expired(cursor)
            cursor.execute(query, params)
            conn.commit()
            return cursor.fetchall() if fetch else None

    def envelope_record_exists(self, envelope_id: str) -> bool:
        q = f"SELECT EXISTS(SELECT 1 FROM {self._sql_ident(self.db_name)} WHERE envelope_id = ?)"
        rows = self.execute_query(q, (envelope_id,), fetch=True)
        if not rows:
            return False
        return bool(rows[0][0])