import pytest

from matching.llm import LLMError, parse_json_response
from matching.parser import Participant
from matching.prompt import FIELD_LABELS, build_user_prompt


def test_parse_json_strips_fences():
    text = 'Here you go:\n```json\n{"groups": [{"name": "A", "member_ids": [1, 2, 3]}]}\n```'
    assert parse_json_response(text) == {"groups": [{"name": "A", "member_ids": [1, 2, 3]}]}


def test_parse_json_raises_on_garbage():
    with pytest.raises(LLMError):
        parse_json_response("sorry, I cannot do that")


def test_user_prompt_includes_weights_and_roster():
    people = [Participant(id=0, name="Ana Testova"), Participant(id=1, name="Ben Example")]
    prompt = build_user_prompt(people, {"topic": 5}, 4, 3, 6, ["Neuroscience"])
    assert FIELD_LABELS["topic"] in prompt and "importance 5/5" in prompt
    assert "Ana Testova" in prompt and "Neuroscience" in prompt


def test_meet_link_flagged_in_block():
    a = Participant(id=0, name="Ana Testova", who_to_meet="I'd love to meet Ben Example")
    b = Participant(id=1, name="Ben Example")
    prompt = build_user_prompt([a, b], {}, 4, 3, 6, [])
    block_a = prompt.split("### Applicant 0")[1].split("### Applicant 1")[0]
    assert "Explicitly named" in block_a and "Ben Example (applicant 1)" in block_a
    assert "Explicitly named" not in prompt.split("### Applicant 1")[1]
