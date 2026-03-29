import os
import re
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.constants import LOCAL_TEMPLATES_FOLDER
from src.logger import *

_GOOGLE_NATIVE_EXPORT_MIME: dict[str, str] = {
    'application/vnd.google-apps.document': 'application/pdf',
}
_EXPORT_SUFFIX_BY_MIME: dict[str, str] = {
    'application/pdf': '.pdf',
}
_GOOGLE_FOLDER_MIME = 'application/vnd.google-apps.folder'
# Word files are converted to Google Doc format server-side then exported as PDF.
_WORD_MIME_TYPES = {
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}


class Google:
    def __init__(self, refresh_token: str, client_id: str, client_secret: str, spreadsheet_id: str, tab_name: str):
        self.spreadsheet_id = spreadsheet_id
        self.tab_name = tab_name

        creds = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=[
                'https://www.googleapis.com/auth/drive',
                'https://www.googleapis.com/auth/spreadsheets',
            ],
        )
        self.service_google_drive = build('drive', 'v3', credentials=creds)
        self.service_google_sheet = build('sheets', 'v4', credentials=creds)

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

    def _iter_files_recursive(self, root_folder_id: str) -> Iterator[tuple[dict[str, str], tuple[str, ...]]]:
        stack: list[tuple[str, tuple[str, ...]]] = [(root_folder_id, tuple())]
        while stack:
            folder_id, rel_parts = stack.pop()
            page_token = None
            while True:
                response = self.service_google_drive.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields='nextPageToken, files(id, name, mimeType, modifiedTime)',
                    pageToken=page_token,
                ).execute()

                for file_meta in response.get('files', []):
                    mime_type = file_meta.get('mimeType', '')
                    if mime_type == _GOOGLE_FOLDER_MIME:
                        next_rel_parts = rel_parts + (file_meta.get('name', file_meta['id']),)
                        stack.append((file_meta['id'], next_rel_parts))
                    else:
                        yield file_meta, rel_parts

                page_token = response.get('nextPageToken')
                if not page_token:
                    break

    def _make_flat_local_name(
        self,
        *,
        name: str,
        rel_parts: tuple[str, ...],
        export_mime: str | None,
        used_names: set[str],
    ) -> str:
        safe_name = re.sub(r'[/\\]+', '_', name).strip() or 'untitled'
        if export_mime:
            export_suffix = _EXPORT_SUFFIX_BY_MIME.get(export_mime, '')
            if export_suffix and not safe_name.lower().endswith(export_suffix):
                safe_name += export_suffix

        if safe_name not in used_names:
            used_names.add(safe_name)
            return safe_name

        safe_rel = '__'.join(re.sub(r'[/\\]+', '_', part).strip() for part in rel_parts if part.strip())
        base_candidate = f'{safe_rel}__{safe_name}' if safe_rel else safe_name
        candidate = base_candidate
        i = 2
        while candidate in used_names:
            stem, dot, ext = base_candidate.rpartition('.')
            candidate = f'{stem}_{i}.{ext}' if dot else f'{base_candidate}_{i}'
            i += 1

        used_names.add(candidate)
        return candidate

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

    def validate_sheet_headers(self, tab_name: str, expected_columns: tuple[str, ...]) -> None:
        '''Verify that all expected_columns exist in the sheet header row (case-insensitive).
        Raises ValueError listing any missing columns.
        '''
        res = self.service_google_sheet.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f'{tab_name}!1:1',
        ).execute()
        header_row: list[str] = res.get('values', [[]])[0] if res.get('values') else []
        header_lower: set[str] = {h.lower() for h in header_row}
        missing = [col for col in expected_columns if col.lower() not in header_lower]
        if missing:
            raise ValueError(f'Missing columns in sheet header: {missing}')
        InfoLogger(f'Sheet header validation passed for tab [{tab_name}]: all {len(expected_columns)} columns present')

    def append_row_to_sheet(self, tab_name: str, data: dict[str, str]) -> bool:
        '''Appends a row to the sheet, aligned to the header row.
        Keys in `data` are matched case-insensitively against the header.
        None values are written as empty string without mutating the input dict.
        '''
        try:
            res = self.service_google_sheet.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f'{tab_name}!1:1',
            ).execute()
            header_row: list[str] = res.get('values', [[]])[0] if res.get('values') else []

            data_normalised: dict[str, str] = {k.lower(): v for k, v in data.items()}
            header_lower: set[str] = {h.lower() for h in header_row}

            unmatched = [k for k in data if k.lower() not in header_lower]
            if unmatched:
                ErrorLogger(f'failed to append row to Google Sheet: column name not found in header: {unmatched}')
                return False

            # Build row aligned to header order; missing or None values become ''
            row = [
                (data_normalised[h.lower()] if data_normalised[h.lower()] is not None else '')
                if h.lower() in data_normalised else ''
                for h in header_row
            ]

            self.service_google_sheet.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=tab_name,
                valueInputOption='RAW',
                insertDataOption='INSERT_ROWS',
                body={'values': [row]},
            ).execute()
            InfoLogger(f'Appended row to spreadsheet [{self.spreadsheet_id}] tab [{tab_name}]: {data}')
            return True
        except Exception as e:
            ErrorLogger(f'failed to append row to Google Sheet: {e}')
            return False

    def download_templates(self, drive_url: str) -> bool:
        '''
        TODO: Use Google Drive webhook notification to automatically
        download/update template files instead of fetching on every webhook call.
        '''
        folder_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', urlparse(drive_url).path)
        if not folder_match:
            ErrorLogger(f'download_templates: could not parse folder ID from URL: {drive_url}')
            return False
        folder_id = folder_match.group(1)

        try:
            self.service_google_drive.files().get(fileId=folder_id, fields='id').execute()
        except HttpError as e:
            if e.resp.status == 403:
                ErrorLogger(f'download_templates: not authorised to access Google Drive folder [{folder_id}]')
            else:
                ErrorLogger(f'download_templates: Google Drive folder [{folder_id}] is inaccessible: {e}')
            return False

        dest_dir = Path(LOCAL_TEMPLATES_FOLDER)
        dest_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        used_names: set[str] = set()
        try:
            for file_meta, rel_parts in self._iter_files_recursive(folder_id):
                file_id = file_meta['id']
                name = file_meta.get('name', file_id)
                mime_type = file_meta.get('mimeType', '')

                export_mime = None
                temp_copy_id: str | None = None
                if mime_type in _GOOGLE_NATIVE_EXPORT_MIME:
                    export_mime = _GOOGLE_NATIVE_EXPORT_MIME[mime_type]
                    request = self.service_google_drive.files().export_media(
                        fileId=file_id,
                        mimeType=export_mime,
                    )
                elif mime_type in _WORD_MIME_TYPES:
                    # Convert via Google Drive: copy to Google Doc format, export as PDF.
                    copy = self.service_google_drive.files().copy(
                        fileId=file_id,
                        body={'mimeType': 'application/vnd.google-apps.document'},
                    ).execute()
                    temp_copy_id = copy['id']
                    export_mime = 'application/pdf'
                    request = self.service_google_drive.files().export_media(
                        fileId=temp_copy_id,
                        mimeType=export_mime,
                    )
                elif mime_type.startswith('application/vnd.google-apps.'):
                    WarnLogger(f'download_templates: skipping unsupported Google Apps file [{name}] ({mime_type})')
                    continue
                elif mime_type != 'application/pdf':
                    WarnLogger(f'download_templates: skipping unsupported file type [{name}] ({mime_type})')
                    continue
                else:
                    request = self.service_google_drive.files().get_media(fileId=file_id)

                local_name = self._make_flat_local_name(
                    name=name,
                    rel_parts=rel_parts,
                    export_mime=export_mime,
                    used_names=used_names,
                )
                local_path = dest_dir / local_name
                try:
                    with local_path.open('wb') as fh:
                        downloader = MediaIoBaseDownload(fh, request)
                        done = False
                        while not done:
                            _, done = downloader.next_chunk()
                finally:
                    if temp_copy_id:
                        try:
                            self.service_google_drive.files().delete(fileId=temp_copy_id).execute()
                        except Exception:
                            pass

                count += 1
                rel_path = '/'.join(rel_parts)
                if rel_path:
                    InfoLogger(f'download_templates: downloaded [{rel_path}/{name}] to [{local_path}]')
                else:
                    InfoLogger(f'download_templates: downloaded [{name}] to [{local_path}]')
        except HttpError as e:
            ErrorLogger(f'download_templates: failed to list files in folder [{folder_id}]: {e}')
            return False

        InfoLogger(f'download_templates: downloaded [{count}] template(s)')
        return True
