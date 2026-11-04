"""Pass 3: a judge settles the records where passes 1 and 2 disagree."""
import json
from pathlib import Path

from .bigquery_source import MISC_OPTION, SourceRow
from .claude_tagger import NONE_OPTION, first_pass_output, is_misc

FINAL_SYSTEM_PROMPT = (Path(__file__).parent / "third_tag_system_prompt.md").read_text(encoding="utf-8")


def _second_pass_output(row) -> dict:
    tag = (row["tag_2"] or "").strip()
    name = (row["suggested_name_2"] or "").strip()
    answer, brand_label_name = tag, ""
    if is_misc(tag):
        answer = MISC_OPTION
    elif name and tag.lower() == name.lower():
        answer, brand_label_name = NONE_OPTION, name
    return {"correct_answer": answer, "correct_brand_label_name": brand_label_name,
            "correct_is_alcoholic": row["is_alcoholic_2"], "skip_reason": row["skip_reason_2"] or "",
            "explanation": row["reason_2"] or ""}


def disputed_request(row) -> tuple[SourceRow, str]:
    """`row` is a first_second_tag_comparison row. Returns the record to judge and both passes' outputs."""
    options, first = first_pass_output({
        "options": row["options"], "tag": row["tag_1"], "selected_option": row["selected_option_1"],
        "suggested_name": row["suggested_name_1"], "is_alcoholic": row["is_alcoholic_1"],
        "skip_reason": row["skip_reason_1"], "judgements": row["judgements_1"], "reason": row["reason_1"]})
    note = (f"<model_1_output>\n{json.dumps(first, ensure_ascii=False)}\n</model_1_output>\n\n"
            f"<model_2_output>\n{json.dumps(_second_pass_output(row), ensure_ascii=False)}\n</model_2_output>")
    return SourceRow(str(row["source_row_id"]), row["base_value"], options, None), note
