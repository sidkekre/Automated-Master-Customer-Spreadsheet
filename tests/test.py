import unittest
import os
from pathlib import Path
from datetime import datetime, timedelta

from src.db.db import DB_WITH_TTL

from src.constants import (
    TEST_DB_PATH,
    TEST_DB_NAME,
    TEST_TTL_DAYS,
    TEST_ENVELOPE_ID,
)

class TestDB(unittest.TestCase):
    def setUp(self):
        self.db = DB_WITH_TTL(
            TEST_DB_PATH,
            TEST_DB_NAME,
            TEST_TTL_DAYS,
        )

    def tearDown(self):
        db_file = TEST_DB_PATH + TEST_DB_NAME + '.db'
        if os.path.exists(db_file):
            os.remove(db_file)

    def test_envelope_exists_after_insert(self):
        self.db.execute_query(
            f"""INSERT OR IGNORE INTO {self.db._sql_ident(TEST_DB_NAME)} (envelope_id, created_at)
                VALUES (?, ?)""",
            (TEST_ENVELOPE_ID, datetime.now()),
        )
        self.assertTrue(
            self.db.envelope_record_exists(TEST_ENVELOPE_ID)
        )

    def test_records_purge_after_ttl(self):
        self.db.execute_query(
            f"""INSERT OR IGNORE INTO {self.db._sql_ident(TEST_DB_NAME)} (envelope_id, created_at)
                VALUES (?, ?)""",
            (TEST_ENVELOPE_ID, datetime.now() - timedelta(days=TEST_TTL_DAYS + 1)),
        )
        self.assertFalse(
            self.db.envelope_record_exists(TEST_ENVELOPE_ID)
        )


if __name__ == "__main__":
    unittest.main()