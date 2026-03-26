import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src.constants import (
    TEST_DB_PATH,
    TEST_DB_NAME,
    TEST_TTL_DAYS,
    TEST_ENVELOPE_ID,
    TEST_DOCUSIGN_HMAC_SECRET,
    TEST_DOCUSIGN_SIGNATURE_HEADER,
    TEST_DOCUSIGN_UNKNOWN_EVENT,
    DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
    DOCUSIGN_SIGNATURE_HEADER,
    STATUS_PROCESSED,
    STATUS_DUPLICATE,
    STATUS_IGNORED,
)
from src.data_sources.docusign import DocuSignConnectHandler
from src.db.db import DB_WITH_TTL


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
            f"""INSERT OR IGNORE INTO {self.db._sql_ident(TEST_DB_NAME)} (envelope_id, event_type, created_at) VALUES (?, ?, ?)""",
            (TEST_ENVELOPE_ID, DOCUSIGN_EVENT_ENVELOPE_COMPLETED, datetime.now().isoformat()),
        )
        self.assertTrue(self.db.envelope_record_exists(TEST_ENVELOPE_ID))

    def test_records_purge_after_ttl(self):
        old_timestamp = (datetime.now() - timedelta(days=TEST_TTL_DAYS + 1)).isoformat()
        self.db.execute_query(
            f"""INSERT OR IGNORE INTO {self.db._sql_ident(TEST_DB_NAME)} (envelope_id, event_type, created_at) VALUES (?, ?, ?)""",
            (TEST_ENVELOPE_ID, DOCUSIGN_EVENT_ENVELOPE_COMPLETED, old_timestamp),
        )
        self.assertFalse(self.db.envelope_record_exists(TEST_ENVELOPE_ID))


class TestDocuSignConnectHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.test_db_file = TEST_DB_PATH + TEST_DB_NAME + '.db'
        self.db = DB_WITH_TTL(TEST_DB_PATH, TEST_DB_NAME, TEST_TTL_DAYS)
        self.handler = DocuSignConnectHandler(envelope_db=self.db)

    def tearDown(self) -> None:
        if os.path.exists(self.test_db_file):
            os.remove(self.test_db_file)

    def test_processes_completed_envelope_once(self) -> None:
        payload = {
            'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
            'data': {'envelopeId': TEST_ENVELOPE_ID},
        }

        result = self.handler.handle_connect_webhook(payload)

        self.assertEqual(result.get('status'), STATUS_PROCESSED)
        self.assertEqual(result.get('event'), DOCUSIGN_EVENT_ENVELOPE_COMPLETED)
        self.assertEqual(result.get('envelope_id'), TEST_ENVELOPE_ID)

    def test_rejects_duplicate_completed_envelope(self) -> None:
        self.db.execute_query(
            f"INSERT OR IGNORE INTO {self.db._sql_ident(TEST_DB_NAME)} (envelope_id, event_type, created_at) VALUES (?, ?, ?)",
            (TEST_ENVELOPE_ID, DOCUSIGN_EVENT_ENVELOPE_COMPLETED, datetime.now().isoformat()),
        )

        payload = {
            'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
            'data': {'envelopeId': TEST_ENVELOPE_ID},
        }

        result = self.handler.handle_connect_webhook(payload)

        self.assertEqual(result.get('status'), STATUS_DUPLICATE)
        self.assertEqual(result.get('event'), DOCUSIGN_EVENT_ENVELOPE_COMPLETED)
        self.assertEqual(result.get('envelope_id'), TEST_ENVELOPE_ID)

    def test_unknown_event_handling(self) -> None:
        payload = {
            'event': TEST_DOCUSIGN_UNKNOWN_EVENT,
            'data': {'envelopeId': TEST_ENVELOPE_ID},
        }

        result = self.handler.handle_connect_webhook(payload)

        self.assertEqual(result.get('status'), STATUS_IGNORED)
        self.assertEqual(result.get('event'), TEST_DOCUSIGN_UNKNOWN_EVENT)
        self.assertEqual(result.get('envelope_id'), TEST_ENVELOPE_ID)


class TestWebhookRoute(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {'DOCUSIGN_HMAC_SECRET': TEST_DOCUSIGN_HMAC_SECRET}):
            from src.main import app
            self.app = app
            self.client = self.app.test_client()

    def test_webhook_rejects_invalid_signature(self) -> None:
        webhook_body = json.dumps({
            'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
            'data': {'envelopeId': TEST_ENVELOPE_ID},
        }).encode()

        response = self.client.post(
            '/docusign-notification',
            data=webhook_body,
            content_type='application/json',
            headers={DOCUSIGN_SIGNATURE_HEADER: TEST_DOCUSIGN_SIGNATURE_HEADER},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {'error': 'unauthorized'})


if __name__ == '__main__':
    unittest.main()