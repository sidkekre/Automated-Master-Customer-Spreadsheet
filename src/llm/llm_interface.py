from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Dict

from src.logger import InfoLogger

from openai import OpenAI

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().with_name('prompt_template_analysis.md')
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().with_name('prompt_contract_info_extract.md')


class OpenAILLMInterface:
    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        system_prompt_path: str | os.PathLike[str] | None = None,
        output_path: str | os.PathLike[str] | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.system_prompt_path = Path(system_prompt_path or DEFAULT_SYSTEM_PROMPT_PATH).resolve()
        self.output_path = Path(output_path or DEFAULT_OUTPUT_PATH).resolve()
        self.system_prompt = self._load_system_prompt()
        self.client = client or self._build_client(api_key)

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
