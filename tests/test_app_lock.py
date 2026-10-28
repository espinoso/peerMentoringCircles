"""Lock-aware regeneration split (app.split_locked), the one bit of app.py that
is pure and testable without a Streamlit runtime."""
from app import prune_locked, split_locked
from matching.parser import Participant
from matching.prompt import Group


def _p(i: int) -> Participant:
    return Participant(id=i, name=f"P{i}")


def test_locking_leaves_too_few_unlocked():
    active = [_p(i) for i in range(5)]
    groups = [
        Group(name="Locked", member_ids=[0, 1, 2], locked=True),
        Group(name="Rest", member_ids=[3, 4]),
    ]
    locked, pool_ids, too_few = split_locked(groups, active, min_size=3)
    assert [g.name for g in locked] == ["Locked"]
    assert pool_ids == [3, 4]  # locked members removed from the pool
    assert too_few is True


def test_no_locks_regenerates_everyone():
    active = [_p(i) for i in range(6)]
    groups = [
        Group(name="A", member_ids=[0, 1, 2]),
        Group(name="B", member_ids=[3, 4, 5]),
    ]
    locked, pool_ids, too_few = split_locked(groups, active, min_size=3)
    assert locked == []
    assert pool_ids == [0, 1, 2, 3, 4, 5]
    assert too_few is False


def test_prune_locked_drops_excluded_member():
    # Member 2 is in a locked circle but has since been excluded (not active),
    # so it must not survive the re-attach.
    locked = [Group(name="Locked", member_ids=[0, 1, 2], locked=True)]
    kept = prune_locked(locked, active_ids={0, 1})
    assert kept[0].member_ids == [0, 1]  # excluded member 2 dropped


def test_prune_locked_drops_emptied_circle():
    # Every member of a locked circle is now excluded -> the circle is dropped
    # entirely rather than riding along empty.
    locked = [
        Group(name="GoneA", member_ids=[2, 3], locked=True),
        Group(name="Keep", member_ids=[0, 4], locked=True),
    ]
    kept = prune_locked(locked, active_ids={0, 1})
    assert [g.name for g in kept] == ["Keep"]
    assert kept[0].member_ids == [0]
