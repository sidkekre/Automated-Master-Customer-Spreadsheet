import os
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from docusign_esign.client.api_exception import ApiException

from src.db import db
from src.logger import *
from src.data_sources import docusign

from src.constants import (
    DB_PATH,
    DocuSign_Envelope_Records_DB,
    TTL_DAYS,
    DOCUSIGN_SIGNATURE_HEADER,
    STATUS_OK,
    STATUS_ERROR,
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

    try:
        result = docusign_connect.handle_connect_webhook(payload)
    except docusign.DocuSignConsentRequiredError as exc:
        ErrorLogger(f'DocuSign consent required: {exc}')
        body = {'error': 'docusign_consent_required'}
        if exc.consent_url:
            body['consent_url'] = exc.consent_url
        return jsonify(body), 503
    except ApiException as exc:
        ErrorLogger(f'DocuSign API request failed: {exc}')
        return jsonify({'error': 'docusign_request_failed'}), 502
    except ValueError as exc:
        ErrorLogger(f'DocuSign configuration error: {exc}')
        return jsonify({'error': 'docusign_configuration_error'}), 500

    if result.get('status') == STATUS_ERROR:
        return jsonify({'error': result['reason']}), 400

    return jsonify(result), 200


if __name__ == '__main__':
    global FLASK_PORT, HMAC_SECRET, docusign_connect
    _required = [
        'FLASK_PORT',
        'DOCUSIGN_HMAC_SECRET',
        'DOCUSIGN_INTEGRATION_KEY',
        'DOCUSIGN_USER_ID',
        'DOCUSIGN_PRIVATE_KEY',
        'DOCUSIGN_AUTH_URL',
        'DOCUSIGN_CONSENT_REDIRECT_URI',
    ]
    _missing = [k for k in _required if not os.getenv(k)]
    if _missing:
        ErrorLogger(f'Missing required environment variables: {", ".join(_missing)}')
        raise SystemExit(1)

    FLASK_PORT = os.getenv('FLASK_PORT')
    HMAC_SECRET = os.getenv('DOCUSIGN_HMAC_SECRET')
    docusign_connect = docusign.DocuSignConnectHandler(
        integration_key=os.getenv('DOCUSIGN_INTEGRATION_KEY'),
        user_id=os.getenv('DOCUSIGN_USER_ID'),
        private_key=os.getenv('DOCUSIGN_PRIVATE_KEY'),
        envelope_db=envelope_db,
        auth_url=os.getenv('DOCUSIGN_AUTH_URL'),
        consent_redirect_uri=os.getenv('DOCUSIGN_CONSENT_REDIRECT_URI'),
    )
    app.run(host='0.0.0.0', port=FLASK_PORT)