import json

from tagger.__main__ import _retag_request
from tagger.claude_tagger import _result, user_message


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
