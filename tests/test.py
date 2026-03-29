import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

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
    EXTRACTION_COLUMNS,
    STATUS_PROCESSED,
    STATUS_DUPLICATE,
    STATUS_IGNORED,
)
from src.data_sources.docusign import DocuSignConnectHandler
from src.data_sources.google import Google
from src.db.db import DB_WITH_TTL
from src.llm.llm_interface import OpenAILLMInterface


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
        self.handler = DocuSignConnectHandler(
            integration_key='test-key',
            user_id='test-user',
            private_key='test-private-key',
            auth_url='test-auth-url',
            consent_redirect_uri='test-consent-redirect-uri',
            envelope_db=self.db,
        )

    def tearDown(self) -> None:
        if os.path.exists(self.test_db_file):
            os.remove(self.test_db_file)

    @patch.object(DocuSignConnectHandler, 'download_envelope', return_value=(b'%PDF', {'name': 'test', 'envelope_id': TEST_ENVELOPE_ID, 'status': 'completed', 'envelope_type': None}))
    def test_processes_completed_envelope_once(self, mock_download) -> None:
        payload = {
            'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
            'data': {'envelopeId': TEST_ENVELOPE_ID},
        }

        result = self.handler.handle_connect_webhook(payload)

        self.assertEqual(result.get('status'), STATUS_PROCESSED)
        self.assertEqual(result.get('event'), DOCUSIGN_EVENT_ENVELOPE_COMPLETED)
        self.assertEqual(result.get('envelope_id'), TEST_ENVELOPE_ID)
        self.assertIn('local_pdf_path', result)
        self.assertIn('envelope_meta', result)

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
        import src.main
        src.main.HMAC_SECRET = TEST_DOCUSIGN_HMAC_SECRET
        self.app = src.main.app
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        import src.main
        src.main.HMAC_SECRET = None

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


def _build_extraction_json(overrides: dict | None = None) -> str:
    '''Build a valid 32-key JSON string for extraction tests.'''
    base = {col: f'val_{i}' for i, col in enumerate(EXTRACTION_COLUMNS)}
    if overrides:
        base.update(overrides)
    return json.dumps(base)


class TestExtractContractInfo(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = OpenAILLMInterface(api_key='test-key', client=MagicMock())

    @patch.object(OpenAILLMInterface, 'delete_file')
    @patch.object(OpenAILLMInterface, '_complete')
    @patch.object(OpenAILLMInterface, 'upload_file', return_value='file-123')
    def test_returns_dict_with_all_keys_on_valid_json(self, mock_upload, mock_complete, mock_delete) -> None:
        mock_complete.return_value = _build_extraction_json({'Company Name': None})
        result = self.llm.extract_contract_info(Path('/tmp/test.pdf'))
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), len(EXTRACTION_COLUMNS))
        self.assertEqual(result['Company Name'], '')
        mock_delete.assert_called_once_with('file-123')

    @patch.object(OpenAILLMInterface, 'delete_file')
    @patch.object(OpenAILLMInterface, '_complete')
    @patch.object(OpenAILLMInterface, 'upload_file', return_value='file-123')
    def test_returns_none_on_bad_response(self, mock_upload, mock_complete, mock_delete) -> None:
        mock_complete.return_value = 'not valid json'
        result = self.llm.extract_contract_info(Path('/tmp/test.pdf'))
        self.assertIsNone(result)

    @patch.object(OpenAILLMInterface, 'delete_file')
    @patch.object(OpenAILLMInterface, '_complete', side_effect=Exception('API error'))
    @patch.object(OpenAILLMInterface, 'upload_file', return_value='file-123')
    def test_cleans_up_file_on_error(self, mock_upload, mock_complete, mock_delete) -> None:
        result = self.llm.extract_contract_info(Path('/tmp/test.pdf'))
        self.assertIsNone(result)
        mock_delete.assert_called_once_with('file-123')


class TestValidateSheetHeaders(unittest.TestCase):
    @patch('src.data_sources.google.build')
    def setUp(self, mock_build) -> None:
        self.mock_sheets = MagicMock()
        mock_build.side_effect = [MagicMock(), self.mock_sheets]
        self.google = Google(
            refresh_token='test', client_id='test',
            client_secret='test', spreadsheet_id='test-id', tab_name='Legal',
        )
        self.google.service_google_sheet = self.mock_sheets

    def _mock_header_response(self, headers: list[str]) -> None:
        self.mock_sheets.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            'values': [headers]
        }

    def test_passes_when_all_columns_present(self) -> None:
        self._mock_header_response(list(EXTRACTION_COLUMNS) + ['Document Link'])
        self.google.validate_sheet_headers('Legal', EXTRACTION_COLUMNS)

    def test_raises_when_columns_missing(self) -> None:
        self._mock_header_response(['Company Name', 'Entity Type'])
        with self.assertRaises(ValueError) as ctx:
            self.google.validate_sheet_headers('Legal', EXTRACTION_COLUMNS)
        self.assertIn('Missing columns', str(ctx.exception))

    def test_case_insensitive_match(self) -> None:
        headers = [col.upper() for col in EXTRACTION_COLUMNS]
        self._mock_header_response(headers)
        self.google.validate_sheet_headers('Legal', EXTRACTION_COLUMNS)


if __name__ == '__main__':
    unittest.main()