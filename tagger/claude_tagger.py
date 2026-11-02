"""The tagging prompt, the record_tag tool, result handling (shared by every provider) and the Claude caller."""
import re
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

SYSTEM_PROMPT = """You tag retail product records for a liquor catalogue, answering the same question a human tagger would ask Google.

Each record is a question about one item as it appears in a store's data, often abbreviated and with sizes and pack counts. The question offers catalogue names separated by OR, then "Misc - Not a Liquor" and "None of the above", and asks which name most properly identifies the item, whether the item is alcoholic, and what the most proper Brand + Label name is when none of the names identify it.

Which name most properly identifies the item
A catalogue name identifies the item only when it names exactly the same product. These differences are ignored:
- sizes (such as 750ml or 1.75L), pack counts and pack wording (such as 6pk, 12 x 355ml or Pack);
- proof and ABV (such as 100 Proof, 90pf or 40%), unless they are part of the product's name, as in "1792 Full Proof";
- letter case, punctuation and spacing ("W.L. Weller" is "W L Weller");
- standard abbreviations and their full forms ("Yr" is "Year").
The one exception is a variety pack: "Variety Pack" must be in both the item and the catalogue name, or in neither. For the item "Truly Variety Pack 12pk", "Truly Hard Seltzer Variety Pack" can identify it but "Truly Wild Berry" can't.
Everything else must be the same: the brand, product line, flavour/expression and age statement. A name does not identify the item when it names a different brand, adds a word the item doesn't have (such as a parent company or distillery name, but not the liquor type, see below) or lacks one it has, or names a different expression of the same brand, or only the brand. A wrong match is worse than no match.

For example, for the item "1792 Full Proof Bourbon 750ml":
- "1792 Full Proof Bourbon 1.75L" identifies it (only the size differs);
- "Buffalo Trace 1792 Full Proof" does not (it adds Buffalo Trace and lacks Bourbon);
- "W L Weller Full Proof" does not (different brand).
With only the last two offered, the answer is "None of the above" with the Brand + Label name "1792 Full Proof Bourbon".

Liquor type
The tag must say what type of drink the product is, such as tequila, bourbon, whiskey, rum, vodka, gin, brandy, cognac, liqueur, wine, champagne, beer or hard seltzer. A wine's grape or style (such as Cabernet Sauvignon or Prosecco) and a beer's style (such as IPA or Lager) count as its type.
- A catalogue name may add the type when the item leaves it out: for the item "Titos 1.75L", "Tito's Handmade Vodka" identifies it, because Tito's Handmade is a vodka.
- Only pick a catalogue name that contains the type. If the only catalogue name that identifies the item has no type, such as "Tito's Handmade", answer "None of the above" and give the Brand + Label name with the type, "Tito's Handmade Vodka".
- When neither the item nor the catalogue names say the type, search to find it.
This doesn't apply to "Misc - Not a Liquor".

If several catalogue names identify the item equally well, pick the first. Choose "Misc - Not a Liquor" only when the item is clearly not an alcoholic drink: food (such as pizza, noodles, chips or candy), mixers and soft drinks, tobacco, glassware, bags, deposits and so on. Choose "None of the above" when the item is clearly an alcoholic drink you can identify, but no catalogue name identifies it.

Brand + Label name
When the answer is "None of the above", give the product's most proper Brand + Label name, written so it would look like one more catalogue name: Title Case, abbreviations written out, in the usual order of brand, age, product line, expression and type, such as "Whistlepig 6 Year Piggyback Rye Whiskey". It must contain the liquor type. Use the name the brand itself sells the product under. Leave out sizes, pack counts, proof or ABV (unless part of the product's name), store codes and stray punctuation.

Web search
How you decide depends on whether the item names a liquor type, such as tequila, bourbon, whiskey, vodka, rum, gin, brandy, cognac, liqueur, wine, champagne, beer, lager, IPA, hard seltzer or cider:
- If it does, decide from the name alone, without searching.
- If it doesn't, search for the item name and decide from what the results say the product is and what type of drink it is.
Also search when you need the product's real Brand + Label name, or its type for the tag. Don't search to confirm a decision you can already make.

Skipping
Skip the record when it is still unclear after searching: you can't tell what the item is, whether it is alcoholic, what type of drink it is, or whether a catalogue name identifies it. A skipped record goes to a person, so skip rather than guess. To skip, answer "None of the above", leave brand_label_name "" and say in skip_reason what is unclear, such as "search results don't say what WTRK 101 is" or "can't tell whether the item is the 12 Year or the 18 Year". Don't skip just because no catalogue name identifies the item: when you know what the product is, give its Brand + Label name instead.

Answer
When you have decided, record your answer once with record_tag:
- answer: the chosen name copied exactly from the question: one of the catalogue names, "Misc - Not a Liquor" or "None of the above".
- name_reasons: one entry per catalogue name in the question, in order, each a short phrase saying why it does or doesn't identify the item, such as "only the size differs" or "different brand (W L Weller)". Empty when no catalogue names are offered.
- is_alcoholic: whether the item is an alcoholic drink.
- brand_label_name: when the answer is "None of the above" and you aren't skipping, the most proper Brand + Label name. Otherwise "".
- skip_reason: when you skip, one sentence saying what is unclear. Otherwise "".
- confidence: high when the item text clearly identifies the product and the decision is clear-cut, medium when you had to interpret the item text, low when it is too vague to be sure.
- reason: one sentence explaining the final decision."""

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
    return f"{message}\n\n{note}" if note else message


def _key(name: str | None) -> str:
    """Compare names ignoring case, punctuation and spacing."""
    return re.sub(r"[^0-9a-z]+", " ", (name or "").lower()).strip()


def _result(options: list[str], decision: dict, sources: list[dict]) -> TagResult:
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
                       judgements=judgements, sources=sources[:5])

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
                 system_prompt: str = SYSTEM_PROMPT):
        self.llm = llm
        self.model = llm.model
        self.budget = budget
        self.effort = effort
        self.system_prompt = system_prompt
        self.client = anthropic.Anthropic(api_key=llm.api_key, timeout=180.0, max_retries=4)
        self.haiku = "haiku" in llm.model
        tool_type = "web_search_20250305" if self.haiku else "web_search_20260209"
        search = {"type": tool_type, "name": "web_search", "max_uses": 2}
        self.tools = [search, RECORD_TAG_TOOL] if web_search else [RECORD_TAG_TOOL]

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
            if block.type == "tool_use" and block.name == "record_tag":
                return block.input
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
