from __future__ import annotations
from typing import Any, Optional
import base64
import hashlib
import hmac as hmac_module
from urllib.parse import quote, urlparse
from datetime import datetime, timedelta, timezone

from docusign_esign import ApiClient, EnvelopesApi
from docusign_esign.client.api_exception import ApiException

from src.logger import *
from src.constants import (
    DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
    DOCUSIGN_JWT_EXPIRY_SECONDS,
    DOCUSIGN_JWT_REFRESH_BUFFER_SECONDS,
    STATUS_PROCESSED,
    STATUS_ERROR,
    STATUS_IGNORED,
    STATUS_DUPLICATE,
    REASON_MISSING_EVENT,
    REASON_MISSING_ENVELOPE_ID,
)

class DocuSignConsentRequiredError(Exception):
    def __init__(self, message: str, consent_url: str | None = None) -> None:
        super().__init__(message)
        self.consent_url = consent_url

class DocuSignConnectHandler:
    '''
    API References:
    https://support.docusign.com/s/document-item?language=en_US&bundleId=vob1727899215236&topicId=lry1608071169447.html
    https://docusign.github.io/docusign-esign-node-client/module-api_EnvelopesApi.html#getEnvelope
    https://github.com/docusign/docusign-esign-python-client/tree/master/docusign_esign/apis
    '''

    def __init__(self, integration_key: str, user_id: str, private_key: str, auth_url: str, consent_redirect_uri: str, envelope_db: Any = None) -> None:
        self._integration_key = integration_key
        self._user_id = user_id
        self._oauth_host = self._parse_oauth_host(auth_url)
        self._private_key = private_key
        self._consent_redirect_uri = consent_redirect_uri
        self._account_id: str | None = None
        self._api_client: ApiClient | None = None
        self._token_expiry: datetime | None = None
        self.envelope_db = envelope_db

    @staticmethod
    def verify_webhook_hmac(raw_body: bytes, signature_header: str, hmac_secret: str) -> bool:
        expected = base64.b64encode(
            hmac_module.new(hmac_secret.encode(), raw_body, hashlib.sha256).digest()
        ).decode()
        return hmac_module.compare_digest(expected, signature_header)

    @staticmethod
    def _parse_oauth_host(auth_url: str) -> str:
        parsed_host = urlparse(auth_url).hostname
        if parsed_host:
            return parsed_host
        return auth_url.strip().strip('/') or 'account.docusign.com'

    def _get_account(self, accounts: list[Any]) -> Any:
        if not accounts:
            raise ValueError('No DocuSign accounts available for this access token.')

        for account in accounts:
            if str(getattr(account, 'is_default', '')).lower() == 'true':
                return account

        return accounts[0]

    def _build_consent_url(self) -> str | None:
        if not self._integration_key or not self._consent_redirect_uri:
            WarnLogger('Cannot build DocuSign consent URL: missing integration key or consent redirect URI')
            return None
        scope = quote('signature impersonation', safe='')
        client_id = quote(self._integration_key, safe='')
        redirect_uri = quote(self._consent_redirect_uri, safe='')
        return (
            f'https://{self._oauth_host}/oauth/auth'
            f'?response_type=code&scope={scope}&client_id={client_id}&redirect_uri={redirect_uri}'
        )

    def _get_api_client(self) -> ApiClient:
        now = datetime.now(timezone.utc)
        if self._api_client and self._token_expiry and now < self._token_expiry:
            return self._api_client

        api_client = ApiClient()
        api_client.set_oauth_host_name(self._oauth_host)

        try:
            token = api_client.request_jwt_user_token(
                client_id=self._integration_key,
                user_id=self._user_id,
                oauth_host_name=self._oauth_host,
                private_key_bytes=self._private_key.encode(),
                expires_in=DOCUSIGN_JWT_EXPIRY_SECONDS,
                scopes=('signature', 'impersonation'),
            )
        except ApiException as exc:
            if 'consent_required' in str(exc):
                consent_url = self._build_consent_url()
                if consent_url:
                    ErrorLogger(f'DocuSign consent required. Open this URL once to grant consent: {consent_url}')
                raise DocuSignConsentRequiredError(
                    'DocuSign consent is required for JWT impersonation.',
                    consent_url=consent_url,
                ) from exc
            raise
        user_info = api_client.get_user_info(token.access_token)
        account = self._get_account(getattr(user_info, 'accounts', []) or [])

        api_client.host = f"{account.base_uri}/restapi"
        self._account_id = account.account_id
        self._api_client = api_client

        expires_in = int(getattr(token, 'expires_in', DOCUSIGN_JWT_EXPIRY_SECONDS))
        refresh_after = max(expires_in - DOCUSIGN_JWT_REFRESH_BUFFER_SECONDS, 0)
        self._token_expiry = now + timedelta(seconds=refresh_after)

        InfoLogger(f'DocuSign API client configured for account {self._account_id}')
        return self._api_client

    def preflight_auth(self) -> dict[str, Any]:
        self._get_api_client()
        return {
            'ready': True,
            'oauth_host': self._oauth_host,
            'account_id': self._account_id,
        }

    @staticmethod
    def _extract_envelope_type(envelope: Any) -> Optional[str]:
        custom_fields = getattr(envelope, 'custom_fields', None)
        text_custom_fields = getattr(custom_fields, 'text_custom_fields', None) or []

        for field in text_custom_fields:
            if getattr(field, 'name', None) == 'Envelope Type':
                return getattr(field, 'value', None)

        return None
        
    def download_envelope(self, envelope_id: str) -> tuple[bytes, dict[str, Any]]:
        api_client = self._get_api_client()
        envelopes_api = EnvelopesApi(api_client)

        envelope = envelopes_api.get_envelope(
            self._account_id,
            envelope_id,
            include='custom_fields',
        )
        pdf_bytes = envelopes_api.get_document(
            self._account_id,
            'combined',
            envelope_id,
        )

        envelope_meta = {
            'envelope_id': envelope_id,
            'name': envelope.email_subject or '',
            'status': envelope.status or '',
            'envelope_type': self._extract_envelope_type(envelope),
        }

        InfoLogger(f'Downloaded envelope {envelope_id} ({len(pdf_bytes)} bytes)')
        return pdf_bytes, envelope_meta

    @staticmethod
    def get_event_type(payload: dict[str, Any]) -> Optional[str]:
        raw = payload.get('event')
        if not isinstance(raw, str) or not raw.strip():
            return None
        return raw.strip().lower().replace('_', '-')

    @staticmethod
    def parse_webhook_payload(payload: dict[str, Any]) -> Optional[str]:
        data = payload.get('data')
        if not isinstance(data, dict):
            return None

        envelope_id = data.get('envelopeId')
        if not isinstance(envelope_id, str) or not envelope_id.strip():
            return None

        return envelope_id.strip()

    def handle_connect_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        DocuSign Connect POST body: read top-level `event`, then run the matching handler.
        Connect v2.1 JSON includes `event`, `data.envelopeId`, `data.envelopeDocuments.name`, etc.
        """
        raw = payload.get("event")
        if not isinstance(raw, str) or not raw.strip():
            ErrorLogger(f"DocuSign webhook missing event type: {payload}")
            return {"status": STATUS_ERROR, "reason": REASON_MISSING_EVENT}

        event = raw.strip().lower().replace("_", "-")
        if not event:
            return {'status': STATUS_ERROR, 'reason': REASON_MISSING_EVENT}

        envelope_id = self.parse_webhook_payload(payload)
        if envelope_id is None:
            return {'status': STATUS_ERROR, 'reason': REASON_MISSING_ENVELOPE_ID}

        if event == DOCUSIGN_EVENT_ENVELOPE_COMPLETED:
            if self.envelope_db is not None and self.envelope_db.envelope_record_exists(envelope_id):
                WarnLogger(f'Duplicate envelope completed ignored: {envelope_id}')
                return {'status': STATUS_DUPLICATE, 'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED, 'envelope_id': envelope_id}
            return self.handle_envelope_completed(envelope_id)

        WarnLogger(f"DocuSign webhook ignored (unhandled event): {event}")
        return {"status": STATUS_IGNORED, "event": event, "envelope_id": envelope_id}

    def _record_event(self, envelope_id: str, event_type: str) -> None:
        if self.envelope_db is None:
            return
        self.envelope_db.upsert_envelope_event(envelope_id, event_type)

    def handle_envelope_completed(self, envelope_id: str) -> dict[str, Any]:
        # TODO: Add next steps for completed envelopes (e.g. download PDF, upload PDF to Google Drive, update Google Spread Sheet, etc.)
        # TODO: Update Google Spreadsheet ('Status' column to 'Completed')
        InfoLogger(f'Received {DOCUSIGN_EVENT_ENVELOPE_COMPLETED} event for envelope {envelope_id}')
        self._record_event(envelope_id, DOCUSIGN_EVENT_ENVELOPE_COMPLETED)
        return {
            'status': STATUS_PROCESSED,
            'event': DOCUSIGN_EVENT_ENVELOPE_COMPLETED,
            'envelope_id': envelope_id,
        }