import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from docusign_esign.client.api_exception import ApiException

from src.db import db
from src.logger import *
from src.data_sources import docusign
from src.data_sources.google import Google
from src.llm.llm_interface import OpenAILLMInterface

from src.constants import (
    DB_PATH,
    DocuSign_Envelope_Records_DB,
    TTL_DAYS,
    DOCUSIGN_SIGNATURE_HEADER,
    DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
    EXTRACTION_COLUMNS,
    GOOGLE_DRIVE_ROOT_FOLDER,
    STATUS_OK,
    STATUS_PROCESSED,
)

load_dotenv()

app = Flask(__name__)

envelope_db = db.DB_WITH_TTL(
    DB_PATH,
    DocuSign_Envelope_Records_DB,
    TTL_DAYS,
)

@app.route('/health')
def health_check():
    return jsonify({'status': STATUS_OK}), 200

@app.route('/health/docusign-auth')
def docusign_auth_health_check():
    try:
        result = docusign_connect.preflight_auth()
        return jsonify(result), 200
    except docusign.DocuSignConsentRequiredError as exc:
        ErrorLogger(f'DocuSign consent required: {exc}')
        body = {'ready': False, 'reason': 'consent_required'}
        if exc.consent_url:
            body['consent_url'] = exc.consent_url
        return jsonify(body), 503
    except ApiException as exc:
        ErrorLogger(f'DocuSign API auth preflight failed: {exc}')
        return jsonify({'ready': False, 'reason': 'docusign_request_failed'}), 502
    except ValueError as exc:
        ErrorLogger(f'DocuSign configuration error during preflight: {exc}')
        return jsonify({'ready': False, 'reason': 'docusign_configuration_error'}), 500
    except Exception as exc:
        ErrorLogger(f'Unexpected error during DocuSign auth preflight: {exc}')
        return jsonify({'ready': False, 'reason': 'unexpected_error'}), 500

@app.route('/docusign-notification', methods=['POST'])
def docusign_notification_received():
    raw_body = request.get_data()
    signature = request.headers.get(DOCUSIGN_SIGNATURE_HEADER, '')

    if not docusign.DocuSignConnectHandler.verify_webhook_hmac(raw_body, signature, HMAC_SECRET):
        ErrorLogger('DocuSign webhook HMAC verification failed')
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'invalid_payload'}), 400

    main(payload)
    return jsonify({'status': STATUS_OK}), 200


def main(payload: dict) -> None:
    try:
        result = docusign_connect.handle_connect_webhook(payload)
    except docusign.DocuSignConsentRequiredError as e:
        ErrorLogger(f'DocuSign consent required: {e}')
        return
    except ApiException as e:
        ErrorLogger(f'DocuSign API request failed: {e}')
        return
    except ValueError as e:
        ErrorLogger(f'DocuSign configuration error: {e}')
        return
    except Exception as e:
        ErrorLogger(f'Unexpected error processing DocuSign webhook: {e}')
        return

    if result.get('status') == STATUS_PROCESSED and result.get('event') == DOCUSIGN_EVENT_ENVELOPE_COMPLETED:
        pdf_name = result['pdf_name']
        local_pdf_path = result['local_pdf_path']
        envelope_id = result['envelope_id']

        pdf_gdrive_url = google.upload_pdf(local_pdf_path, GOOGLE_DRIVE_ROOT_FOLDER)
        if pdf_gdrive_url is None:
            return

        InfoLogger(f'Envelope [{envelope_id}] PDF [{pdf_name}] uploaded to Google Drive')

        extracted = llm.extract_contract_info(Path(local_pdf_path))
        if extracted is None:
            ErrorLogger(f'Envelope [{envelope_id}] PDF [{pdf_name}] extraction failed')
            return

        extracted['Document Link'] = pdf_gdrive_url
        google.append_row_to_sheet(GOOGLE_SPREADSHEET_TAB_NAME, extracted)

def parse_contract_templates(drive_url: str) -> None:
    if not google.download_templates(drive_url):
        ErrorLogger('failed to download contract templates from Google Drive')
        raise SystemExit(1)
    try:
        llm.generate_extraction_prompt()
        InfoLogger('Contract extraction system prompt generated')
    except Exception as e:
        ErrorLogger(f'failed to generate contract extraction system prompt: {e}')
        raise SystemExit(1)

if __name__ == '__main__':
    global FLASK_PORT, HMAC_SECRET, docusign_connect, google, GOOGLE_SPREADSHEET_TAB_NAME, llm
    _required = [
        'FLASK_PORT',
        'DOCUSIGN_HMAC_SECRET',
        'DOCUSIGN_INTEGRATION_KEY',
        'DOCUSIGN_USER_ID',
        'DOCUSIGN_PRIVATE_KEY',
        'DOCUSIGN_AUTH_URL',
        'DOCUSIGN_CONSENT_REDIRECT_URI',
        'GOOGLE_REFRESH_TOKEN',
        'GOOGLE_CLIENT_ID',
        'GOOGLE_CLIENT_SECRET',
        'GOOGLE_SPREADSHEET_ID',
        'GOOGLE_SPREADSHEET_TAB_NAME',
        'TEMPLATES_GOOGLE_DRIVE_URL',
        'OPENAI_API_KEY',
        'OPENAI_MODEL',
    ]
    _missing = [k for k in _required if not os.getenv(k)]
    if _missing:
        ErrorLogger(f'Missing required environment variables: {", ".join(_missing)}')
        raise SystemExit(1)


    HMAC_SECRET = os.getenv('DOCUSIGN_HMAC_SECRET')

    try:
        docusign_connect = docusign.DocuSignConnectHandler(
            integration_key=os.getenv('DOCUSIGN_INTEGRATION_KEY'),
            user_id=os.getenv('DOCUSIGN_USER_ID'),
            private_key=os.getenv('DOCUSIGN_PRIVATE_KEY'),
            auth_url=os.getenv('DOCUSIGN_AUTH_URL'),
            consent_redirect_uri=os.getenv('DOCUSIGN_CONSENT_REDIRECT_URI'),
            envelope_db=envelope_db,
        )
    except Exception as e:
        ErrorLogger(f'failed to initialize DocuSign handler: {e}')
        raise SystemExit(1)

    try:
        GOOGLE_SPREADSHEET_TAB_NAME = os.getenv('GOOGLE_SPREADSHEET_TAB_NAME')
        google = Google(
            refresh_token=os.getenv('GOOGLE_REFRESH_TOKEN'),
            client_id=os.getenv('GOOGLE_CLIENT_ID'),
            client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
            spreadsheet_id=os.getenv('GOOGLE_SPREADSHEET_ID'),
            tab_name=GOOGLE_SPREADSHEET_TAB_NAME,
        )
    except Exception as e:
        ErrorLogger(f'failed to initialize Google client: {e}')
        raise SystemExit(1)

    try:
        google.validate_sheet_headers(GOOGLE_SPREADSHEET_TAB_NAME, EXTRACTION_COLUMNS)
    except ValueError as e:
        ErrorLogger(f'Google Sheet header mismatch: {e}')
        raise SystemExit(1)
    except Exception as e:
        ErrorLogger(f'failed to validate Google Sheet headers: {e}')
        raise SystemExit(1)

    try:
        llm = OpenAILLMInterface(
            api_key=os.getenv('OPENAI_API_KEY'),
            model=os.getenv('OPENAI_MODEL'),
        )
    except Exception as e:
        ErrorLogger(f'failed to initialize LLM interface: {e}')
        raise SystemExit(1)

    # parse_contract_templates(os.getenv('TEMPLATES_GOOGLE_DRIVE_URL'))

    FLASK_PORT = os.getenv('FLASK_PORT')
    app.run(host='0.0.0.0', port=FLASK_PORT)
