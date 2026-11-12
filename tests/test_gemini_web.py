from types import SimpleNamespace

from tagger.bigquery_source import SourceRow
from tagger.budget import Budget
from tagger.config import LLMConfig
from tagger.gemini_web_tagger import GeminiWebTagger

LLM = LLMConfig("gemini-web", "gemini", "gemini-auto", "x", 0, 0, 0, base_url="http://localhost:4981/openai/v1")
ROW = SourceRow("1", "JACK DANIELS 750ML", ["Jack Daniels Old No. 7", "Jim Beam"], None)


def _tag_with_reply(monkeypatch, reply):
    tagger = GeminiWebTagger(LLM, Budget(1.0))
    fake = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    monkeypatch.setattr(tagger.client.chat.completions, "create", lambda **kw: fake)
    return tagger.tag(ROW)


def test_tags_from_fenced_json_reply(monkeypatch):
    reply = ('Sure!\n```json\n{"answer": "Jack Daniels Old No. 7", "name_reasons": [], "is_alcoholic": true, '
             '"brand_label_name": "", "skip_reason": "", "confidence": "high", "reason": "exact"}\n```')
    assert _tag_with_reply(monkeypatch, reply).tag == "Jack Daniels Old No. 7"


def test_reply_without_json_is_an_error(monkeypatch):
    result = _tag_with_reply(monkeypatch, "I can't help with that.")
    assert result.tag is None and "No decision JSON" in result.error
