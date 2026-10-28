import app
from matching.llm import LLMError
from matching.prompt import Group


def test_progress_fraction_eases_and_caps():
    assert app._progress_fraction(0) == 0.0
    assert 0.4 < app._progress_fraction(50, 100) < 0.6
    assert app._progress_fraction(10_000, 100) == 0.95  # never reaches 1.0
    assert app._progress_fraction(10, 0) == 0.0  # guards expected <= 0


def test_match_worker_records_success(monkeypatch):
    monkeypatch.setattr(app, "compute_match", lambda *a: ([Group("C", [0, 1, 2])], ["a note"]))
    holder: dict = {}
    app._match_worker(holder, {}, [], [], [], "key")
    assert holder["status"] == "done"
    assert holder["notes"] == ["a note"]
    assert holder["groups"][0].name == "C"


def test_match_worker_records_error(monkeypatch):
    def boom(*_a):
        raise LLMError("cut off before it finished")

    monkeypatch.setattr(app, "compute_match", boom)
    holder: dict = {}
    app._match_worker(holder, {}, [], [], [], "key")
    assert holder["status"] == "error"
    assert "cut off" in holder["error"]
