import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.logger import *


class Google:
    def __init__(self, refresh_token: str, client_id: str, client_secret: str):
        creds_drive = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=['https://www.googleapis.com/auth/drive'],
        )
        self.service_google_drive = build('drive', 'v3', credentials=creds_drive)

    def _resolve_local_path(self, local_path):
        return str(Path(local_path).resolve())

    def _get_or_create_folder_id(self, drive_path):
        parent_id = 'root'
        parts = [p for p in drive_path.strip('/').split('/') if p]

        for folder_name in parts:
            query = (
                f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' "
                f"and '{parent_id}' in parents and trashed=false"
            )
            results = self.service_google_drive.files().list(q=query, fields='files(id)').execute()
            items = results.get('files', [])

            if items:
                parent_id = items[0]['id']
            else:
                file_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id],
                }
                folder = self.service_google_drive.files().create(body=file_metadata, fields='id').execute()
                parent_id = folder.get('id')

        return parent_id

    def _set_public_view(self, file_id):
        permission = {'type': 'anyone', 'role': 'reader'}
        self.service_google_drive.permissions().create(fileId=file_id, body=permission).execute()

    def upload_pdf(self, path_to_pdf_local: str, path_of_pdf_in_drive: str) -> str | None:
        '''Returns the public webViewLink URL on success, None on failure.'''
        try:
            abs_local_path = self._resolve_local_path(path_to_pdf_local)
            if not os.path.exists(abs_local_path):
                ErrorLogger(f'local file not found: {abs_local_path}')
                return None

            folder_id = self._get_or_create_folder_id(path_of_pdf_in_drive)

            file_metadata = {
                'name': os.path.basename(abs_local_path),
                'parents': [folder_id],
            }
            media = MediaFileUpload(abs_local_path, mimetype='application/pdf')

            file = self.service_google_drive.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink',
            ).execute()

            self._set_public_view(file.get('id'))

            InfoLogger(f'Upload success: {file.get("webViewLink")}')
            return file.get('webViewLink')
        except Exception as e:
            ErrorLogger(f'failed to upload PDF to Google Drive: {e}')
            return None
