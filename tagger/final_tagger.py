"""Pass 3: a third tagger settles the records where passes 1 and 2 disagree."""
from .bigquery_source import MISC_OPTION, SourceRow
from .claude_tagger import NONE_OPTION, SYSTEM_PROMPT, is_misc

FINAL_SYSTEM_PROMPT = SYSTEM_PROMPT + """

Disputed records
Two earlier taggers disagreed on this record. Their answers are listed after the question, and each earlier answer that names a product is also one of the catalogue names in the question. Decide which is right by the rules above, judging every name on its own merits: an answer isn't more likely to be right because a tagger gave it. Pick one of the earlier answers when it names exactly the same product as the item, or another catalogue name if that one does instead. Answer "None of the above" with a Brand + Label name only when no catalogue name is exactly the same product, and answer "Misc - Not a Liquor" only when the item is clearly not an alcoholic drink. If an earlier tagger skipped the record, its reason is given; settle the record yourself if you can, and skip it with your own reason only if it is still unclear."""


def _describe(tag: str | None, options: list[str], skip_reason: str | None = None) -> str:
    tag = (tag or "").strip()
    if not tag:
        return f"skipped it ({skip_reason})" if skip_reason else "no answer"
    if is_misc(tag):
        return f"not an alcoholic drink ({MISC_OPTION})"
    match = next((o for o in options if o.lower() == tag.lower()), None)
    return match or tag


def disputed_request(row) -> tuple[SourceRow, str]:
    """`row` is a claude_tag_comparison row. Returns the record to tag and the note about earlier answers."""
    options, seen = [], set()
    for option in [*(row["options"] or []), row["tag_1"], row["tag_2"]]:
        option = (option or "").strip()
        key = option.lower()
        if not option or key in ("none", NONE_OPTION.lower()) or is_misc(option) or key in seen:
            continue
        seen.add(key)
        options.append(option)

    note = ("Earlier answers (they disagree):\n"
            f"- Tagger 1: {_describe(row['tag_1'], options, row.get('skip_reason_1'))}\n"
            f"- Tagger 2: {_describe(row['tag_2'], options, row.get('skip_reason_2'))}")
    return SourceRow(str(row["source_row_id"]), row["base_value"], options, None), note
