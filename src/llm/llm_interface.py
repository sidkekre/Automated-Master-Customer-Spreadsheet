from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, List, Dict

from src.constants import LOCAL_TEMPLATES_FOLDER, EXTRACTION_COLUMNS, EXTRACTION_DEFAULTS
from src.logger import InfoLogger, WarnLogger, ErrorLogger

from openai import OpenAI

PROMPT_TEMPLATE_ANALYSIS = Path(__file__).resolve().with_name('prompt_template_analysis.md')
PROMPT_CONTRACT_INFO_EXTRACT = Path(__file__).resolve().with_name('prompt_contract_info_extract.md')


class OpenAILLMInterface:
    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        system_prompt_path: str | os.PathLike[str] | None = None,
        main_system_prompt: str | os.PathLike[str] | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.system_prompt_path = Path(system_prompt_path or PROMPT_TEMPLATE_ANALYSIS).resolve()
        self.main_system_prompt = Path(main_system_prompt or PROMPT_CONTRACT_INFO_EXTRACT).resolve()
        self.system_prompt = self._load_system_prompt()
        self.client = client or self._build_client(api_key)

    def generate_extraction_prompt(self) -> str:
        '''Reads contract template PDFs from LOCAL_TEMPLATES_FOLDER, uploads them to the
        OpenAI Files API, sends them with prompt_template_analysis.md to the LLM, writes
        the generated extraction prompt to prompt_contract_info_extract.md, and returns it.
        All files are expected to be PDFs — download_templates() ensures this.
        Uploaded files are cleaned up regardless of outcome.
        '''
        template_dir = Path(LOCAL_TEMPLATES_FOLDER)
        if not template_dir.exists():
            raise FileNotFoundError(f'Templates directory not found: [{template_dir}]')

        file_ids: List[str] = []

        try:
            for path in sorted(template_dir.iterdir()):
                if not path.is_file():
                    continue
                if path.suffix.lower() != '.pdf':
                    WarnLogger(f'generate_extraction_prompt: skipping non-PDF file [{path.name}]')
                    continue
                file_ids.append(self.upload_file(path))

            if not file_ids:
                raise ValueError(f'No PDF template files found in [{LOCAL_TEMPLATES_FOLDER}]')

            user_content = [{'type': 'input_file', 'file_id': fid} for fid in file_ids]
            result = self._complete(self.system_prompt, user_content)
        finally:
            for fid in file_ids:
                try:
                    self.delete_file(fid)
                except Exception:
                    pass

        self.main_system_prompt.write_text(result, encoding='utf-8')
        InfoLogger(f'Extraction prompt written to [{self.main_system_prompt}]')
        return result

    def extract_contract_info(self, pdf_path: Path) -> dict[str, str] | None:
        '''Upload a signed contract PDF, extract structured fields via LLM, return as dict.
        Returns None on any failure (malformed response, missing keys, API error).
        '''
        extraction_prompt = self.main_system_prompt.read_text(encoding='utf-8').strip()
        file_id: str | None = None
        try:
            file_id = self.upload_file(pdf_path)
            user_content = [{'type': 'input_file', 'file_id': file_id}]
            raw = self._complete(extraction_prompt, user_content)
        except Exception as e:
            ErrorLogger(f'extract_contract_info: LLM call failed for [{pdf_path.name}]: {e}')
            return None
        finally:
            if file_id:
                try:
                    self.delete_file(file_id)
                except Exception:
                    pass

        # Strip markdown fences if present
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            ErrorLogger(f'extract_contract_info: invalid JSON for [{pdf_path.name}]: {e}')
            return None

        if not isinstance(parsed, dict):
            ErrorLogger(f'extract_contract_info: expected JSON object, got {type(parsed).__name__}')
            return None

        expected = set(EXTRACTION_COLUMNS)
        actual = set(parsed.keys())
        missing = expected - actual
        for key in list(missing):
            if key in EXTRACTION_DEFAULTS:
                parsed[key] = EXTRACTION_DEFAULTS[key]
                missing.discard(key)
        if missing:
            ErrorLogger(f'extract_contract_info: missing keys in response: {missing}')
            return None

        # Convert null values to empty string for sheet compatibility
        return {k: ('' if v is None else str(v)) for k, v in parsed.items() if k in expected}

    def upload_file(self, file_path: Path) -> str:
        '''Upload a file to the OpenAI Files API. Returns the file_id.'''
        with file_path.open('rb') as f:
            response = self.client.files.create(file=f, purpose='user_data')
        InfoLogger(f'Uploaded [{file_path.name}]')
        return response.id

    def delete_file(self, file_id: str) -> None:
        '''Delete a previously uploaded file from the OpenAI Files API.'''
        self.client.files.delete(file_id)

    def _complete(self, system_prompt: str, user_content: List[Dict] | str) -> str:
        '''Core LLM I/O. Sends system_prompt + user_content, returns response text.
        user_content may be a plain string or a list of OpenAI input-part dicts.
        '''
        if isinstance(user_content, str):
            user_content = [{'type': 'input_text', 'text': user_content}]

        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    'role': 'system',
                    'content': [{'type': 'input_text', 'text': system_prompt}],
                },
                {
                    'role': 'user',
                    'content': user_content,
                },
            ],
        )

        return self._extract_text(response)

    def _extract_text(self, response: Any) -> str:
        texts: List[str] = []

        for item in getattr(response, 'output', None) or []:
            for content in getattr(item, 'content', None) or []:
                if getattr(content, 'type', None) == 'output_text':
                    text_value = getattr(content, 'text', None)
                    if isinstance(text_value, str) and text_value.strip():
                        texts.append(text_value)

        if not texts:
            output_text = getattr(response, 'output_text', None)
            if isinstance(output_text, str) and output_text.strip():
                texts.append(output_text)

        if not texts:
            raise ValueError('Failed to parse OpenAI response: no text output found')

        return '\n'.join(texts).strip()

    def _build_client(self, api_key: str) -> Any:
        if not api_key:
            raise ValueError('Missing OpenAI API key')
        return OpenAI(api_key=api_key)

    def _load_system_prompt(self) -> str:
        if not self.system_prompt_path.exists():
            raise FileNotFoundError(f'System prompt not found: {self.system_prompt_path}')
        return self.system_prompt_path.read_text(encoding='utf-8').strip()


LLMInterface = OpenAILLMInterface
