"""The same tagger on a local Gemini-web proxy (signs in with a browser cookie) through its OpenAI-style chat API.

The proxy has no function calling or token usage, so the decision tool's fields come back as a JSON object
described in the prompt, and every call costs nothing. Gemini searches the web on its own; no sources come back.
"""
import json
import random
import threading
import time

import openai

from .bigquery_source import SourceRow
from .budget import Budget
from .claude_tagger import (RECORD_TAG_TOOL, SYSTEM_PROMPT, TaggingError, TagResult, _result, candidates,
                            clean_item, parse_json, user_message)
from .config import LLMConfig

GAP = (4.0, 9.0)  # seconds between the starts of two calls, picked at random each time


class GeminiWebTagger:
    def __init__(self, llm: LLMConfig, budget: Budget, effort: str = "medium", web_search: bool = True,
                 system_prompt: str = SYSTEM_PROMPT, tool: dict = RECORD_TAG_TOOL):
        self.llm = llm
        self.model = llm.model
        self.budget = budget
        # effort and web_search can't be set through the proxy; Gemini picks them itself.
        self.system_prompt = (system_prompt + f"\n\nGive {tool['name']}'s fields as a single JSON object and nothing "
                              f"else, matching this JSON schema:\n{json.dumps(tool['input_schema'])}")
        # max_retries backs off on the proxy's 429s (30 requests a minute by default)
        self.client = openai.OpenAI(base_url=llm.base_url, api_key=llm.api_key, timeout=300.0, max_retries=6)
        self._gap_lock = threading.Lock()
        self._next_call = 0.0

    def tag(self, row: SourceRow, note: str | None = None) -> TagResult:
        options = candidates(row)
        message = user_message(clean_item(row.base_value), options, note)
        try:
            return _result(options, self._decision(self._request(message)), [])
        except TaggingError as e:
            return TagResult(tag=None, error=str(e))
        except openai.APIStatusError as e:
            return TagResult(tag=None, error=f"API error {e.status_code}: {e.message}")
        except openai.APIConnectionError as e:
            return TagResult(tag=None, error=f"Connection error (is the proxy at {self.llm.base_url} running?): {e}")

    def _wait_turn(self) -> None:
        """Space calls GAP seconds apart across all workers, so the cookie session looks less like a bot."""
        with self._gap_lock:
            wait = self._next_call - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_call = time.monotonic() + random.uniform(*GAP)

    def _request(self, message: str) -> str:
        self.budget.check()
        self._wait_turn()
        response = self.client.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": self.system_prompt},
                                        {"role": "user", "content": message}])
        return response.choices[0].message.content if response.choices else ""

    @staticmethod
    def _decision(text: str) -> dict:
        try:
            return parse_json(text)
        except ValueError:
            raise TaggingError(f"No decision JSON in the reply: {(text or '')[:200]!r}")
