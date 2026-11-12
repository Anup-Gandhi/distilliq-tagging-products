import json
import subprocess
import sys

from types import SimpleNamespace

from tagger.__main__ import _retag_request, _tag_rows
from tagger.budget import BudgetExceeded
from tagger.claude_tagger import ClaudeTagger, _result, user_message
from tagger.final_tagger import disputed_request


def first(**kw):
    return {"source_row_id": "1", "base_value": "Titos 1L", "options": ["A Vodka", "B Vodka"], "tag": "A Vodka",
            "selected_option": "A Vodka", "suggested_name": None, "is_alcoholic": True, "skip_reason": None,
            "judgements": [{"option": "A Vodka", "matches": True, "reason": "same"}], "confidence": "high",
            "reason": "r", "source_insert_dttm": None, **kw}


def output(note):
    return json.loads(note.split("\n")[1])


def review(verdict="DISAGREE", answer="None of the above", name="", alcoholic=True):
    return {"verdict": verdict, "correct_answer": answer, "correct_brand_label_name": name,
            "correct_is_alcoholic": alcoholic, "explanation": "why"}


def test_chosen_option_is_shown_as_the_answer():
    row, note = _retag_request(first())
    assert row.options == ["A Vodka", "B Vodka"]
    assert output(note)["answer"] == "A Vodka"
    assert output(note)["name_reasons"] == [{"name": "A Vodka", "reason": "same"}]


def test_suggested_name_is_taken_back_out_of_the_catalogue_names():
    row, note = _retag_request(first(options=["A Vodka", "Titos Vodka"], tag="Titos Vodka", selected_option=None,
                                     suggested_name="Titos Vodka"))
    assert row.options == ["A Vodka"]
    assert (output(note)["answer"], output(note)["brand_label_name"]) == ("None of the above", "Titos Vodka")


def test_old_others_row_and_misc_row():
    row, note = _retag_request(first(options=["A Vodka"], tag="Others: Titos Vodka", selected_option=None))
    assert (output(note)["answer"], output(note)["brand_label_name"]) == ("None of the above", "Titos Vodka")
    _, note = _retag_request(first(tag="<Misc - not a Liquor>", selected_option=None, is_alcoholic=False))
    assert output(note)["answer"] == "Misc - Not a Liquor"


def test_tag_rows_yields_every_row_and_stops_on_budget():
    assert sorted(_tag_rows(range(10), lambda r: r * 2, 2)) == [(r, r * 2) for r in range(10)]

    def over(_):
        raise BudgetExceeded()
    # one window of workers x 4 is already queued, then no more are started
    assert sorted(_tag_rows(range(100), over, 1)) == [(r, None) for r in range(4)]


def test_ctrl_c_stops_without_waiting_for_workers():
    code = ("import signal, threading, time\n"
            "from tagger.__main__ import _stop_on_ctrl_c, _tag_rows\n"
            "_stop_on_ctrl_c()\n"
            "threading.Timer(0.5, signal.raise_signal, [signal.SIGINT]).start()\n"
            "list(_tag_rows(range(4), lambda r: time.sleep(30), 4))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert done.returncode == 130, done.stderr
    assert "Stopped." in done.stderr


def test_row_with_no_first_tag_can_be_reviewed():
    for kw in ({"skip_reason": "no brand"}, {"judgements": None, "reason": None, "confidence": None}):
        row, note = _retag_request(first(tag=None, selected_option=None, is_alcoholic=None, **{"judgements": [], **kw}))
        assert row.options == ["A Vodka", "B Vodka"]
        assert (output(note)["answer"], output(note)["skip_reason"]) == ("", kw.get("skip_reason", ""))
    # The reviewer can name what pass 1 skipped, or skip it too.
    options = ["A Vodka"]
    assert _result(options, review(name="Titos Vodka"), []).tag == "Titos Vodka"
    assert _result(options, review("AGREE", ""), []).skip_reason == "why"


def test_message_wraps_the_question_only_for_a_review():
    message = user_message("Titos", ["A Vodka"], "<model_1_output>\n{}\n</model_1_output>")
    assert message.startswith("<question>\n") and "</question>\n\n<model_1_output>" in message
    assert user_message("Titos", ["A Vodka"], "Earlier answers").startswith("Which name")


def test_review_results():
    options = ["A Vodka"]
    assert _result(options, review("AGREE", "A Vodka"), []).tag == "A Vodka"
    named = _result(options, review(name="Titos Vodka"), [])
    assert (named.tag, named.suggested_name, named.reason) == ("Titos Vodka", "Titos Vodka", "why")
    assert _result(options, review(answer="Misc - Not a Liquor", alcoholic=False), []).tag == "Misc - Not a Liquor"
    assert _result(options, review("IGNORED", "", alcoholic=None), []).skip_reason == "why"
    # A reviewer that calls pass 1's name a guess gives no name: a skip, not an error.
    unclear = _result(options, review(), [])
    assert (unclear.tag, unclear.skip_reason, unclear.error) == (None, "why", None)


def disputed(**kw):
    return {"source_row_id": "1", "base_value": "Titos 1L", "options": ["A Vodka", "Titos Vodka"], "tag_1": "Titos Vodka",
            "tag_2": "A Vodka", "selected_option_1": None, "suggested_name_1": "Titos Vodka", "suggested_name_2": None,
            "is_alcoholic_1": True, "is_alcoholic_2": True, "skip_reason_1": None, "skip_reason_2": None,
            "judgements_1": [], "reason_1": "first", "reason_2": "second", **kw}


def judge(decision="MODEL_1", tag="None of the above", name="Titos Vodka", alcoholic=True):
    return {"dispute_type": "MATCH", "disputed_point": "point", "decision": decision, "final_tag": tag,
            "final_brand_label_name": name, "final_is_alcoholic": alcoholic, "explanation": "why"}


def test_disputed_request_shows_real_names_and_both_outputs():
    row, note = disputed_request(disputed())
    assert row.options == ["A Vodka"]
    assert note.startswith("<model_1_output>\n") and "</model_1_output>\n\n<model_2_output>\n" in note
    first, second = json.loads(note.split("\n")[1]), json.loads(note.split("\n")[5])
    assert (first["answer"], first["brand_label_name"]) == ("None of the above", "Titos Vodka")
    assert (second["correct_answer"], second["explanation"]) == ("A Vodka", "second")
    _, note = disputed_request(disputed(tag_2="New Vodka", suggested_name_2="New Vodka"))
    second = json.loads(note.split("\n")[5])
    assert (second["correct_answer"], second["correct_brand_label_name"]) == ("None of the above", "New Vodka")
    _, note = disputed_request(disputed(tag_2=None, skip_reason_2="item text has no brand", is_alcoholic_2=None))
    second = json.loads(note.split("\n")[5])
    assert (second["correct_answer"], second["skip_reason"]) == ("", "item text has no brand")
    # Pass 1 skipped and pass 2 never ran: still a valid request, showing both have no answer.
    row, note = disputed_request(disputed(tag_1=None, tag_2=None, suggested_name_1=None, skip_reason_1="no brand"))
    first, second = json.loads(note.split("\n")[1]), json.loads(note.split("\n")[5])
    assert (first["answer"], first["skip_reason"]) == ("", "no brand")
    assert second["skip_reason"] == "the second pass gave no result"


def test_judge_results():
    options = ["A Vodka"]
    named = _result(options, judge(), [])
    assert (named.tag, named.decision, named.dispute_type, named.disputed_point) == (
        "Titos Vodka", "MODEL_1", "MATCH", "point")
    assert _result(options, judge("MODEL_2", "A Vodka", ""), []).selected_option == "A Vodka"
    assert _result(options, judge("MODEL_2", "Misc - Not a Liquor", "", False), []).tag == "Misc - Not a Liquor"
    human = _result(options, judge("HUMAN", "", "", None), [])
    assert (human.tag, human.skip_reason, human.decision, human.error) == (None, "why", "HUMAN", None)
    # Model 2 skipped and won: the skip stands, whatever it said about alcohol.
    for alcoholic in (None, False):
        won = _result(options, judge("MODEL_2", "", "", alcoholic), [])
        assert (won.tag, won.skip_reason, won.error) == (None, "why", None)


def test_json_written_as_text_counts_as_the_answer():
    fenced = '```json\n{"verdict": "AGREE", "correct_answer": "A Vodka"}\n```'
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=fenced)])
    assert ClaudeTagger._decision(response)["verdict"] == "AGREE"
    try:
        ClaudeTagger._decision(SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="no")]))
    except Exception as e:
        assert "No decision recorded" in str(e)
    else:
        raise AssertionError("expected a TaggingError")


def test_rerun_table_name_is_checked_before_touching_the_database():
    import pytest
    from tagger.store import Store
    for bad in ("llm_tags", "csv_records", "Bad-Name", "x; drop table llm_tags", ""):
        with pytest.raises(ValueError):
            Store.tags_table(None, bad)
