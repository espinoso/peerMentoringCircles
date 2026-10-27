from pathlib import Path

import pytest

from matching.parser import ParseError, find_duplicates, parse_participants

FIX = Path(__file__).parent / "fixtures"
CORE = {"name", "email", "organization", "topic", "groups"}


def load(fn):
    return parse_participants((FIX / fn).read_bytes(), fn)


@pytest.mark.parametrize("fn", ["responses_2025.csv", "responses_2027.csv"])
def test_parses_headers(fn):
    people, colmap = load(fn)
    assert CORE <= set(colmap)  # title row sits above the header row in both fixtures
    assert len(people) == 8
    assert people[0].name == "Ana Testova"


def test_detects_header_row_under_title():
    # If the title row were taken as header, required columns would be missing -> ParseError.
    people, _ = load("responses_2027.csv")
    assert all(p.email for p in people)


def test_strips_theme_numbering():
    people, _ = load("responses_2027.csv")
    ben = people[1]
    assert "Technology and Robotics" in ben.groups
    assert "13. Technology and Robotics" not in ben.groups
    assert "Psychology/ Behavioral Sciences/Social Sciences" in ben.groups
    assert not any(g[:1].isdigit() for p in people for g in p.groups)


def test_missing_themes_is_empty_list():
    people, _ = load("responses_2025.csv")
    assert people[6].groups == []


def test_wrong_file_raises_parseerror():
    with pytest.raises(ParseError):
        parse_participants(b"foo,bar\n1,2\n3,4\n", "x.csv")


def test_duplicate_email_detected():
    people, _ = load("responses_2025.csv")
    clusters = find_duplicates(people)
    assert len(clusters) == 1
    assert clusters[0].reason == "same e-mail"
    assert {p.name for p in clusters[0].members} == {"Ben Fakeman"}
    assert clusters[0].suggested_keep_id == clusters[0].members[-1].id
