DB_PATH = r'./src/db/'
DocuSign_Envelope_Records_DB = r'DocuSign_Envelope_Records'
TTL_DAYS = 7

DOCUSIGN_EVENT_ENVELOPE_COMPLETED = "envelope-completed"
DOCUSIGN_SIGNATURE_HEADER = 'X-DocuSign-Signature-1'
DOCUSIGN_JWT_EXPIRY_SECONDS = 3600
DOCUSIGN_JWT_REFRESH_BUFFER_SECONDS = 60

STATUS_OK = 'ok'
STATUS_PROCESSED = 'processed'
STATUS_ERROR = 'error'
STATUS_IGNORED = 'ignored'
STATUS_DUPLICATE = 'duplicate'
REASON_MISSING_EVENT = 'missing_event'
REASON_MISSING_ENVELOPE_ID = 'missing_envelope_id'

# TEST ONLY                          

TEST_DB_PATH = r'./tests/'
TEST_DB_NAME = r'TEST_DB'
TEST_TTL_DAYS = 1
TEST_ENVELOPE_ID = "00000000-0000-0000-0000-000000000000"

TEST_DOCUSIGN_HMAC_SECRET = 'test-hmac-secret-key'
TEST_DOCUSIGN_SIGNATURE_HEADER = 'invalid-signature'
TEST_DOCUSIGN_UNKNOWN_EVENT = 'unknown-event'