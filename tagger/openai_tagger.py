"""The same tagger on OpenAI's Responses API."""
import json

import openai

from .bigquery_source import SourceRow
from .budget import CACHE_READ, Budget
from .claude_tagger import (DECISION_TOOLS, RECORD_TAG_TOOL, SYSTEM_PROMPT, TaggingError, TagResult, _result,
                            candidates, clean_item, parse_json, user_message)
from .config import LLMConfig


class OpenAITagger:
    def __init__(self, llm: LLMConfig, budget: Budget, effort: str = "medium", web_search: bool = True,
                 system_prompt: str = SYSTEM_PROMPT, tool: dict = RECORD_TAG_TOOL):
        self.llm = llm
        self.model = llm.model
        self.budget = budget
        self.effort = effort
        self.system_prompt = system_prompt
        self.client = openai.OpenAI(api_key=llm.api_key, timeout=180.0, max_retries=4)
        function = {"type": "function", "name": tool["name"], "description": tool["description"], "strict": True,
                    "parameters": tool["input_schema"]}
        self.tools = [{"type": "web_search"}, function] if web_search else [function]

    def tag(self, row: SourceRow, note: str | None = None) -> TagResult:
        options = candidates(row)
        message = user_message(clean_item(row.base_value), options, note)
        try:
            response = self._request(message)
            return _result(options, self._decision(response), self._sources(response))
        except TaggingError as e:
            return TagResult(tag=None, error=str(e))
        except openai.APIStatusError as e:
            return TagResult(tag=None, error=f"API error {e.status_code}: {e.message}")
        except openai.APIConnectionError as e:
            return TagResult(tag=None, error=f"Connection error: {e}")

    def _request(self, message: str):
        extra = {} if self.model.startswith(("gpt-4", "gpt-3")) else {"reasoning": {"effort": self.effort}}
        self.budget.check()
        response = self.client.responses.create(
            model=self.model, max_output_tokens=16000, instructions=self.system_prompt, input=message,
            tools=self.tools, include=["web_search_call.action.sources"], **extra)
        self.budget.charge(self._cost(response))
        return response

    def _cost(self, response) -> float:
        u = response.usage
        cached = (getattr(u.input_tokens_details, "cached_tokens", 0) or 0) if u.input_tokens_details else 0
        price_in, price_out = self.llm.input_price, self.llm.output_price
        # output_tokens includes reasoning tokens
        tokens = ((u.input_tokens - cached) * price_in + cached * price_in * CACHE_READ
                  + u.output_tokens * price_out) / 1_000_000
        searches = sum(1 for item in response.output if item.type == "web_search_call")
        return tokens + searches * self.llm.search_price

    @staticmethod
    def _decision(response) -> dict:
        for item in response.output:
            if item.type == "function_call" and item.name in DECISION_TOOLS:
                try:
                    return json.loads(item.arguments)
                except json.JSONDecodeError as e:
                    raise TaggingError(f"{item.name} arguments aren't valid JSON: {e}")
        # "Return only this JSON" sometimes gets the JSON written out instead of sent to the tool.
        try:
            return parse_json(response.output_text)
        except ValueError:
            raise TaggingError(f"No decision recorded (status={response.status})")

    @staticmethod
    def _sources(response) -> list[dict]:
        sources, seen = [], set()
        for item in response.output:
            action = getattr(item, "action", None)
            if item.type != "web_search_call" or getattr(action, "type", None) != "search":
                continue
            for source in getattr(action, "sources", None) or []:
                url = getattr(source, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"title": getattr(source, "title", None) or url, "url": url})
        return sources[:5]
