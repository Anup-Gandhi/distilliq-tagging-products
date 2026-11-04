"""The tagging prompt, the record_tag tool, result handling (shared by every provider) and the Claude caller."""
import json
import re
from pathlib import Path
from dataclasses import dataclass, field

import anthropic

from .bigquery_source import MISC_OPTION, SourceRow
from .budget import CACHE_READ, CACHE_WRITE, Budget
from .config import LLMConfig

NONE_OPTION = "None of the above"

# Modelled on the question the taggers asked Google's AI Overview.
QUESTION = ("Which name most properly identifies {item} - {names} ? "
            "Is {item} an alcoholic or non-alcoholic item ? "
            "If none of the names identify {item}, what is the most proper Brand + Label name for {item} ?")
SEPARATOR = " OR "

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")
RETAG_SYSTEM_PROMPT = (Path(__file__).parent / "retag_system_prompt.md").read_text(encoding="utf-8")

RECORD_TAG_TOOL = {
    "name": "record_tag",
    "description": "Record which name identifies the item. Call exactly once, after any searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "name_reasons": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["name", "reason"], "additionalProperties": False}},
            "is_alcoholic": {"type": "boolean"},
            "brand_label_name": {"type": "string"},
            "skip_reason": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["answer", "name_reasons", "is_alcoholic", "brand_label_name", "skip_reason", "confidence",
                     "reason"],
        "additionalProperties": False,
    },
}

RECORD_REVIEW_TOOL = {
    "name": "record_review",
    "description": "Return your review JSON by calling this tool. Call exactly once, after any searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["AGREE", "DISAGREE", "IGNORED"]},
            "correct_answer": {"type": "string"},
            "correct_brand_label_name": {"type": "string"},
            "correct_is_alcoholic": {"type": ["boolean", "null"]},  # null when IGNORED
            "explanation": {"type": "string"},
        },
        "required": ["verdict", "correct_answer", "correct_brand_label_name", "correct_is_alcoholic", "explanation"],
        "additionalProperties": False,
    },
}

RECORD_DECISION_TOOL = {
    "name": "record_decision",
    "description": "Return your decision JSON by calling this tool. Call exactly once, after any searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "dispute_type": {"type": "string",
                             "enum": ["SPELLING", "EXPRESSION", "PRODUCT", "MATCH", "ALCOHOLIC", "SKIP", "FORMAT"]},
            "disputed_point": {"type": "string"},
            "decision": {"type": "string", "enum": ["MODEL_1", "MODEL_2", "HUMAN"]},
            "final_tag": {"type": "string"},
            "final_brand_label_name": {"type": "string"},
            "final_is_alcoholic": {"type": ["boolean", "null"]},  # null when HUMAN
            "explanation": {"type": "string"},
        },
        "required": ["dispute_type", "disputed_point", "decision", "final_tag", "final_brand_label_name",
                     "final_is_alcoholic", "explanation"],
        "additionalProperties": False,
    },
}
DECISION_TOOLS = (RECORD_TAG_TOOL["name"], RECORD_REVIEW_TOOL["name"], RECORD_DECISION_TOOL["name"])

MAX_CONTINUATIONS = 3


class TaggingError(Exception):
    pass


@dataclass
class TagResult:
    tag: str | None                     # the final answer; None on error
    selected_option: str | None = None  # the chosen candidate, if one was chosen
    is_alcoholic: bool | None = None
    suggested_name: str | None = None   # the Brand + Label name
    confidence: str | None = None       # "high" | "medium" | "low"
    reason: str | None = None
    judgements: list[dict] = field(default_factory=list)  # one per candidate: {"option", "matches", "reason"}
    sources: list[dict] = field(default_factory=list)     # up to 5 web pages: {"title", "url"}
    error: str | None = None
    skip_reason: str | None = None      # set when the record was too unclear to tag; tag is then None
    decision: str | None = None         # pass 3 only: MODEL_1 | MODEL_2 | HUMAN
    dispute_type: str | None = None     # pass 3 only
    disputed_point: str | None = None   # pass 3 only


def is_misc(option: str | None) -> bool:
    """True for `Misc - Not a Liquor`, also spelled `<Misc - not a Liquor>`."""
    return bool(option) and option.replace("<", "").replace(">", "").strip().lower() == MISC_OPTION.lower()


def candidates(row: SourceRow) -> list[str]:
    """The catalogue names shown to the model."""
    return [o for o in row.options if o.lower() != "none" and not is_misc(o) and o.lower() != NONE_OPTION.lower()]


def clean_item(text: str) -> str:
    return " ".join(text.split())


def user_message(item: str, options: list[str], note: str | None = None) -> str:
    names = SEPARATOR.join([*options, MISC_OPTION, NONE_OPTION])
    message = QUESTION.format(item=item, names=names)
    if note and note.startswith("<model_1_output>"):  # the input format retag_system_prompt.md describes
        return f"<question>\n{message}\n</question>\n\n{note}"
    return f"{message}\n\n{note}" if note else message


def _key(name: str | None) -> str:
    """Compare names ignoring case, punctuation and spacing."""
    return re.sub(r"[^0-9a-z]+", " ", (name or "").lower()).strip()


def first_pass_output(first: dict) -> tuple[list[str], dict]:
    """The real catalogue names and pass 1's answer, rebuilt from its stored row, for passes 2 and 3 to read."""
    options = list(first["options"] or [])
    tag = (first["tag"] or "").strip()
    name = tag[len("Others:"):].strip() if tag.lower().startswith("others:") else tag
    answer, brand_label_name = first["selected_option"] or tag, ""
    if is_misc(name):
        answer = MISC_OPTION
    elif not first["selected_option"] and (name != tag or name == first["suggested_name"]):
        # Pass 1 made up this name. save() stored it among the options, but it isn't a catalogue name.
        answer, brand_label_name = NONE_OPTION, name
        options = [o for o in options if o.lower() != name.lower()]
    return options, {"answer": answer, "is_alcoholic": first["is_alcoholic"], "brand_label_name": brand_label_name,
                     "skip_reason": first["skip_reason"] or "",
                     "name_reasons": [{"name": j["option"], "reason": j["reason"]}
                                      for j in first["judgements"] or []],
                     "reason": first["reason"]}


def parse_json(text: str) -> dict:
    """The JSON object in a reply, allowing for a code fence or text around it. Raises ValueError if there is none."""
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


def _from_review(review: dict) -> dict:
    """Turn a record_review answer into record_tag's fields."""
    answer, name = review.get("correct_answer") or "", review.get("correct_brand_label_name") or ""
    is_alcoholic, explanation = review.get("correct_is_alcoholic"), review.get("explanation") or ""
    # IGNORED, no answer, or an alcoholic item it can't name: there's nothing to tag, so it's a skip like any other.
    unclear = review["verdict"] == "IGNORED" or (not name and (
        not _key(answer) or (_key(answer) == _key(NONE_OPTION) and is_alcoholic is not False)))
    return {"answer": answer, "brand_label_name": name, "is_alcoholic": is_alcoholic, "reason": explanation,
            "skip_reason": (explanation or "unclear") if unclear else ""}


def _from_judge(judge: dict) -> dict:
    """Turn a record_decision answer into record_tag's fields. HUMAN is saved as a skip."""
    explanation = judge.get("explanation") or ""
    if judge.get("decision") == "HUMAN":
        return {"reason": explanation, "skip_reason": explanation or "needs human review"}
    return _from_review({"verdict": "DISAGREE", "correct_answer": judge.get("final_tag"),
                         "correct_brand_label_name": judge.get("final_brand_label_name"),
                         "correct_is_alcoholic": judge.get("final_is_alcoholic"), "explanation": explanation})


def _result(options: list[str], decision: dict, sources: list[dict]) -> TagResult:
    judge = decision if "dispute_type" in decision else {}
    if judge:
        decision = _from_judge(judge)
    elif "verdict" in decision:
        decision = _from_review(decision)
    answer = (decision.get("answer") or "").strip()
    name = (decision.get("brand_label_name") or "").strip() or None
    is_alcoholic = decision.get("is_alcoholic")

    reasons = decision.get("name_reasons") or []
    by_name = {_key(r.get("name")): r.get("reason", "") for r in reasons}

    def reason_for(n: int, option: str) -> str:
        if _key(option) in by_name:
            return by_name[_key(option)]
        return reasons[n].get("reason", "") if n < len(reasons) else ""

    chosen = next((o for o in options if _key(o) == _key(answer)), None)
    judgements = [{"option": o, "matches": o == chosen, "reason": reason_for(n, o)} for n, o in enumerate(options)]
    result = TagResult(tag=None, is_alcoholic=is_alcoholic, suggested_name=name,
                       confidence=decision.get("confidence"), reason=decision.get("reason"),
                       judgements=judgements, sources=sources[:5], decision=judge.get("decision"),
                       dispute_type=judge.get("dispute_type"), disputed_point=judge.get("disputed_point"))

    skip_reason = (decision.get("skip_reason") or "").strip()
    if skip_reason:
        result.selected_option = result.suggested_name = None
        result.skip_reason = skip_reason
    elif chosen is not None:
        result.tag = result.selected_option = chosen
    elif is_misc(answer) or (_key(answer) == _key(NONE_OPTION) and is_alcoholic is False):
        result.tag = MISC_OPTION
    elif _key(answer) == _key(NONE_OPTION) or (name and _key(answer) == _key(name)):
        if name:
            result.tag = name
        else:
            result.error = "No name fit and no Brand + Label name was suggested"
    else:
        result.error = f"Chose '{answer}', which wasn't one of the names offered"
    return result


class ClaudeTagger:
    def __init__(self, llm: LLMConfig, budget: Budget, effort: str = "medium", web_search: bool = True,
                 system_prompt: str = SYSTEM_PROMPT, tool: dict = RECORD_TAG_TOOL):
        self.llm = llm
        self.model = llm.model
        self.budget = budget
        self.effort = effort
        self.system_prompt = system_prompt
        self.client = anthropic.Anthropic(api_key=llm.api_key, timeout=180.0, max_retries=4)
        self.haiku = "haiku" in llm.model
        tool_type = "web_search_20250305" if self.haiku else "web_search_20260209"
        search = {"type": tool_type, "name": "web_search", "max_uses": 2}
        self.tools = [search, tool] if web_search else [tool]

    def tag(self, row: SourceRow, note: str | None = None) -> TagResult:
        options = candidates(row)
        message = user_message(clean_item(row.base_value), options, note)
        try:
            response = self._request(message)
            return _result(options, self._decision(response), self._sources(response))
        except TaggingError as e:
            return TagResult(tag=None, error=str(e))
        except anthropic.APIStatusError as e:
            return TagResult(tag=None, error=f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return TagResult(tag=None, error=f"Connection error: {e}")

    def _request(self, message: str):
        messages = [{"role": "user", "content": message}]
        extra = {} if self.haiku else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        for _ in range(1 + MAX_CONTINUATIONS):
            self.budget.check()
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=self.system_prompt, messages=messages,
                tools=self.tools, output_config={"effort": self.effort}, **extra)
            self.budget.charge(self._cost(response))
            if response.stop_reason != "pause_turn":
                break
            # A long server-side search paused the turn: send it back unchanged to continue.
            messages = [*messages, {"role": "assistant", "content": response.content}]
        return response

    def _cost(self, response) -> float:
        u = response.usage
        price_in, price_out = self.llm.input_price, self.llm.output_price
        tokens = ((u.input_tokens or 0) * price_in
                  + (getattr(u, "cache_creation_input_tokens", 0) or 0) * price_in * CACHE_WRITE
                  + (getattr(u, "cache_read_input_tokens", 0) or 0) * price_in * CACHE_READ
                  + (u.output_tokens or 0) * price_out) / 1_000_000
        server = getattr(u, "server_tool_use", None)
        searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
        return tokens + searches * self.llm.search_price

    @staticmethod
    def _decision(response) -> dict:
        if response.stop_reason == "refusal":
            raise TaggingError("Claude declined to tag this record")
        for block in response.content:
            if block.type == "tool_use" and block.name in DECISION_TOOLS:
                return block.input
        # "Return only this JSON" sometimes gets the JSON written out instead of sent to the tool.
        try:
            return parse_json("".join(block.text for block in response.content if block.type == "text"))
        except ValueError:
            raise TaggingError(f"No decision recorded (stop_reason={response.stop_reason})")

    @staticmethod
    def _sources(response) -> list[dict]:
        sources, seen = [], set()
        for block in response.content:
            if block.type != "web_search_tool_result" or not isinstance(block.content, list):
                continue  # an error result is a single object, not a list
            for page in block.content:
                url = getattr(page, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"title": getattr(page, "title", None) or url, "url": url})
        return sources[:5]
