"""Tests for the deterministic balancer (matching/balance.py).

The balancer is deterministic given its input, so every case here is
hand-built and asserts an exact invariant — no randomness.
"""
from matching.balance import _affinity, balance_groups
from matching.parser import Participant
from matching.prompt import Group


def P(pid, groups=None, org="", meet="", name="", topic="", keywords=""):
    return Participant(
        id=pid,
        name=name or f"P{pid}",
        groups=list(groups or []),
        organization=org,
        who_to_meet=meet,
        topic=topic,
        keywords=keywords,
    )


def _all_ids(groups):
    ids = []
    for g in groups:
        ids.extend(g.member_ids)
    return ids


def test_everyone_placed_exactly_once():
    # One oversized circle, one undersized circle, and one orphan (id 9).
    ps = [P(i, groups=["A" if i % 2 else "B"]) for i in range(10)]
    groups = [
        Group(name="C1", member_ids=[0, 1, 2, 3, 4, 5, 6]),
        Group(name="C2", member_ids=[7, 8]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(10)))
    placed = _all_ids(out)
    assert sorted(placed) == list(range(10))          # all present
    assert len(placed) == len(set(placed))            # exactly once


def test_all_circles_within_min_max():
    ps = [P(i, groups=["A" if i % 2 else "B"]) for i in range(10)]
    groups = [
        Group(name="C1", member_ids=[0, 1, 2, 3, 4, 5, 6]),
        Group(name="C2", member_ids=[7, 8]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(10)))
    for g in out:
        assert 3 <= len(g.member_ids) <= 6


def test_same_org_separated_when_avoidable():
    # Two people from the same org dumped in the same circle; the other circle
    # has swap partners from distinct orgs, so the clash is avoidable.
    ps = [
        P(0, groups=["T"], org="acme"),
        P(1, groups=["T"], org="acme"),
        P(2, groups=["T"], org="c2"),
        P(3, groups=["T"], org="d"),
        P(4, groups=["T"], org="e"),
        P(5, groups=["T"], org="f"),
    ]
    groups = [
        Group(name="C1", member_ids=[0, 1, 2]),
        Group(name="C2", member_ids=[3, 4, 5]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(6)))
    circle_of = {m: gi for gi, g in enumerate(out) for m in g.member_ids}
    assert circle_of[0] != circle_of[1]


def test_named_meet_request_honored():
    # P0 asks to meet "Bob Jones" (P1), but the model split them apart.
    ps = [
        P(0, groups=["T"], name="Alice Smith", meet="Bob Jones"),
        P(1, groups=["T"], name="Bob Jones"),
        P(2, groups=["T"], name="Cara Lee"),
        P(3, groups=["T"], name="Dan Ng"),
        P(4, groups=["T"], name="Eve Ray"),
        P(5, groups=["T"], name="Fay Poe"),
    ]
    groups = [
        Group(name="C1", member_ids=[0, 2, 3]),
        Group(name="C2", member_ids=[1, 4, 5]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(6)))
    circle_of = {m: gi for gi, g in enumerate(out) for m in g.member_ids}
    assert circle_of[0] == circle_of[1]


def test_tiny_cohort_terminates():
    # Only 2 applicants but min circle size is 3: must return, not raise,
    # place both, and warn that the circle is outside 3-6.
    ps = [P(0, groups=["T"]), P(1, groups=["T"])]
    out, notes = balance_groups([Group(name="C1", member_ids=[0, 1])], ps, [0, 1])
    assert sorted(_all_ids(out)) == [0, 1]
    assert any("3" in n and "6" in n for n in notes)


def test_leftover_member_joins_related_circle():
    # Orphan o shares a theme with the fuller "AI" circle and nothing with the
    # emptier "BIO" circle. With target below the AI circle's size, naive
    # placement would dump o into the emptier (below-target) BIO circle.
    ai = [P(i, groups=["AI"]) for i in range(4)]          # ids 0..3
    bio = [P(i, groups=["BIO"]) for i in range(4, 7)]     # ids 4..6
    o = P(7, groups=["AI"])
    ps = ai + bio + [o]
    groups = [
        Group(name="AI", member_ids=[0, 1, 2, 3]),
        Group(name="BIO", member_ids=[4, 5, 6]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(8)), target=4)
    circle_of = {m: gi for gi, g in enumerate(out) for m in g.member_ids}
    ai_gi = circle_of[0]
    assert circle_of[7] == ai_gi  # leftover joined the themed circle

    # Coherence proxy: affinity to the assigned circle >= to the one it dodged.
    aff_ai = _affinity(o, ai)
    aff_bio = _affinity(o, bio)
    assert aff_ai >= aff_bio


def test_evicted_member_joins_related_circle():
    # An oversized circle (7 > max 6) of theme "X" plus one AI outsider: the
    # outsider is the worst fit and gets evicted. Two circles have room — an
    # "AI" circle at target (4) sharing its theme, and an emptier unrelated
    # "BIO" circle below target (3). With target<max, the old ordering (prefer
    # below-target first) would dump the evictee into BIO; the fixed ordering
    # (highest affinity first) sends it to the themed AI circle.
    x = [P(i, groups=["X"]) for i in range(6)]            # ids 0..5
    outsider = P(6, groups=["AI"])                        # worst fit in the X circle
    ai = [P(i, groups=["AI"]) for i in range(7, 11)]      # ids 7..10 (at target 4)
    bio = [P(i, groups=["BIO"]) for i in range(11, 14)]   # ids 11..13 (emptier, below target)
    ps = x + [outsider] + ai + bio
    groups = [
        Group(name="X", member_ids=[0, 1, 2, 3, 4, 5, 6]),
        Group(name="AI", member_ids=[7, 8, 9, 10]),
        Group(name="BIO", member_ids=[11, 12, 13]),
    ]
    out, _notes = balance_groups(groups, ps, list(range(14)), target=4)
    circle_of = {m: gi for gi, g in enumerate(out) for m in g.member_ids}
    assert circle_of[6] == circle_of[7]  # evictee joined the related AI circle

    # Affinity proxy: the evictee fits its themed landing circle better.
    assert _affinity(outsider, ai) >= _affinity(outsider, bio)
