"""The same tagger on Gemini, searching with Grounding with Google Search (the source of AI Overviews).

Gemini can't combine Google Search with function calling, so record_tag's fields come back as a JSON
object instead: with a response schema on Gemini 3, or described in the prompt on Gemini 2.x, which
doesn't allow a schema together with search.
"""
import json
import re

import httpx
from google import genai
from google.genai import errors, types

from .bigquery_source import SourceRow
from .budget import CACHE_READ, Budget
from .claude_tagger import (RECORD_TAG_TOOL, SYSTEM_PROMPT, TaggingError, TagResult, _result, candidates,
                            clean_item, user_message)
from .config import LLMConfig

JSON_ANSWER = """

Give record_tag's fields as a single JSON object and nothing else, in this form:
{"answer": "...", "name_reasons": [{"name": "...", "reason": "..."}], "is_alcoholic": true, "brand_label_name": "...", "skip_reason": "", "confidence": "high", "reason": "..."}"""

DECLINED = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}
THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}  # Gemini 2.x


def _schema(schema):
    """The record_tag schema without the keys Gemini's response schema doesn't take."""
    if isinstance(schema, dict):
        return {k: _schema(v) for k, v in schema.items() if k not in ("additionalProperties", "strict")}
    if isinstance(schema, list):
        return [_schema(v) for v in schema]
    return schema


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        text = text[start:end + 1] if start != -1 else text
    decision = json.loads(text)
    if not isinstance(decision, dict):
        raise ValueError("not a JSON object")
    return decision


class GeminiTagger:
    def __init__(self, llm: LLMConfig, budget: Budget, effort: str = "medium", web_search: bool = True,
                 system_prompt: str = SYSTEM_PROMPT):
        self.llm = llm
        self.model = llm.model
        self.budget = budget
        self.client = genai.Client(api_key=llm.api_key, http_options=types.HttpOptions(
            timeout=180_000, retry_options=types.HttpRetryOptions(attempts=5)))
        self.gemini_2 = llm.model.startswith("gemini-2")

        config = {"system_instruction": system_prompt, "max_output_tokens": 16000,
                  "thinking_config": self._thinking(effort)}
        if web_search:
            config["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if web_search and self.gemini_2:
            config["system_instruction"] = system_prompt + JSON_ANSWER
        else:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = _schema(RECORD_TAG_TOOL["input_schema"])
        self.config = types.GenerateContentConfig(**config)

    def _thinking(self, effort: str) -> types.ThinkingConfig:
        if self.gemini_2:
            return types.ThinkingConfig(thinking_budget=THINKING_BUDGETS[effort])
        # Gemini 3 Pro only takes low or high; Flash also takes medium.
        level = effort if "flash" in self.model or effort != "medium" else "high"
        return types.ThinkingConfig(thinking_level=level.upper())

    def tag(self, row: SourceRow, note: str | None = None) -> TagResult:
        options = candidates(row)
        message = user_message(clean_item(row.base_value), options, note)
        try:
            response = self._request(message)
            return _result(options, self._decision(response), self._sources(response))
        except TaggingError as e:
            return TagResult(tag=None, error=str(e))
        except errors.APIError as e:
            return TagResult(tag=None, error=f"API error {e.code}: {e.message}")
        except httpx.TransportError as e:
            return TagResult(tag=None, error=f"Connection error: {e}")

    def _request(self, message: str):
        self.budget.check()
        response = self.client.models.generate_content(model=self.model, contents=message, config=self.config)
        self.budget.charge(self._cost(response))
        return response

    def _searches(self, response) -> int:
        candidate = response.candidates[0] if response.candidates else None
        metadata = getattr(candidate, "grounding_metadata", None)
        return len(getattr(metadata, "web_search_queries", None) or [])

    def _cost(self, response) -> float:
        u = response.usage_metadata
        if u is None:
            return 0.0
        cached = u.cached_content_token_count or 0
        prompt = (u.prompt_token_count or 0) + (u.tool_use_prompt_token_count or 0)
        output = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        price_in, price_out = self.llm.input_price, self.llm.output_price
        tokens = ((prompt - cached) * price_in + cached * price_in * CACHE_READ + output * price_out) / 1_000_000
        searches = self._searches(response)
        if self.gemini_2:  # billed per grounded prompt, not per query
            searches = min(searches, 1)
        return tokens + searches * self.llm.search_price

    @staticmethod
    def _decision(response) -> dict:
        if not response.candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None)
            raise TaggingError(f"Gemini declined to tag this record (block_reason={reason})")
        finish = response.candidates[0].finish_reason
        finish_name = getattr(finish, "name", str(finish))
        if finish_name in DECLINED:
            raise TaggingError(f"Gemini declined to tag this record (finish_reason={finish_name})")
        parts = getattr(response.candidates[0].content, "parts", None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        try:
            return _parse_json(text)
        except ValueError:
            raise TaggingError(f"No decision recorded (finish_reason={finish_name})")

    @staticmethod
    def _sources(response) -> list[dict]:
        candidate = response.candidates[0] if response.candidates else None
        metadata = getattr(candidate, "grounding_metadata", None)
        sources, seen = [], set()
        for chunk in getattr(metadata, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            url = getattr(web, "uri", None)
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": getattr(web, "title", None) or url, "url": url})
        return sources[:5]
