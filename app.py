"""IMFAHE Peer Mentoring Circles — matching app.

Flow: log in -> upload the 2027 form export -> review parsed applicants ->
tune per-question weights & group sizes -> generate circles with an LLM ->
edit (move people, rename, add/remove circles) -> download a grouped XLSX.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import streamlit as st

from matching.balance import balance_groups
from matching.export import build_xlsx
from matching.llm import LLMClient, LLMError
from matching.parser import (
    ParseError,
    Participant,
    find_duplicates,
    find_unusable,
    parse_participants,
)
from matching.prompt import Group, generate_groups
from themes import THEMES

try:  # tomllib is stdlib on 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

CONFIG_PATH = Path(__file__).parent / "config.toml"

# Weight sliders, in the same order as the participation form.
WEIGHT_FIELDS = [
    ("organization", "Organization (Q3)"),
    ("position", "Position (Q4)"),
    ("topic", "Specific topic (Q5)"),
    ("keywords", "Keywords (Q6)"),
    ("who_to_meet", "Who they want to meet (Q7)"),
    ("objectives", "Objectives (Q8)"),
    ("groups", "Chosen themes (Q9)"),
    ("multidisciplinary", "Multidisciplinary pref (Q10)"),
    ("comments", "Comments (Q11)"),
]


@st.cache_data
def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def get_secret(key: str, default: str = "") -> str:
    """Read from Streamlit secrets first, then environment."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # noqa: BLE001, S110 - secrets file may not exist locally
        pass
    return os.environ.get(key, default)


def check_password() -> bool:
    """Single shared 'magic word' gate. Set APP_PASSWORD in secrets/env."""
    expected = get_secret("APP_PASSWORD")
    if not expected:  # no password configured -> open access (local dev)
        return True
    if st.session_state.get("authed"):
        return True
    st.title("IMFAHE Peer Mentoring Circles")
    pw = st.text_input("Enter the access word", type="password")
    if pw:
        if pw == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect. Try again.")
    return False


def init_state(cfg: dict):
    st.session_state.setdefault("participants", None)
    st.session_state.setdefault("groups", None)
    st.session_state.setdefault("excluded_ids", set())
    st.session_state.setdefault("weights", dict(cfg["weights"]))


# --------------------------------------------------------------------------- UI


def sidebar_controls(cfg: dict) -> dict:
    st.sidebar.header("Matching settings")

    model = cfg["provider"]["model"]

    st.sidebar.subheader("Group size")
    m = cfg["matching"]
    col1, col2, col3 = st.sidebar.columns(3)
    min_size = col1.number_input("Min", 2, 10, int(m["min_group_size"]))
    ideal = col2.number_input("Ideal", 2, 10, int(m["ideal_group_size"]))
    max_size = col3.number_input("Max", 2, 12, int(m["max_group_size"]))

    st.sidebar.caption(
        "Circles mix institutions by design — people from the same organization are "
        "kept in different circles whenever sizes allow."
    )

    st.sidebar.subheader("Question weights (0–5)")
    st.sidebar.caption("Higher weight = that answer counts for more when forming circles.")
    weights = {}
    for key, label in WEIGHT_FIELDS:
        weights[key] = st.sidebar.slider(
            label, 0, 5, int(st.session_state["weights"].get(key, 0)), key=f"w_{key}"
        )
    st.session_state["weights"] = weights

    return {
        "model": model,
        "min_size": int(min_size),
        "ideal": int(ideal),
        "max_size": int(max_size),
        "weights": weights,
    }


def upload_section() -> None:
    st.subheader("1. Upload the participation-form responses")
    st.caption("Export the 2027 Google Form responses as CSV or XLSX, then upload here.")
    file = st.file_uploader("Responses file", type=["csv", "xlsx", "xls"])
    if file is not None:
        # The uploader returns the same file on every rerun. Only (re)parse when
        # a genuinely different file is uploaded — otherwise we would wipe the
        # generated circles and manual edits on every interaction.
        sig = (file.name, file.size)
        if st.session_state.get("uploaded_sig") != sig:
            try:
                participants, _ = parse_participants(file.getvalue(), file.name)
            except ParseError as e:
                st.error(str(e))
                return
            st.session_state["participants"] = participants
            st.session_state["groups"] = None
            st.session_state["excluded_ids"] = set()
            st.session_state["uploaded_sig"] = sig
            st.success(f"Loaded {len(participants)} applicants.")

    participants = st.session_state.get("participants")
    if participants:
        with st.expander(f"Preview {len(participants)} applicants", expanded=False):
            st.dataframe(
                [
                    {
                        "Name": p.name,
                        "Org": p.organization,
                        "Themes": ", ".join(p.groups),
                        "Topic": p.topic,
                    }
                    for p in participants
                ],
                use_container_width=True,
                hide_index=True,
            )


def _render_full_submission(p: Participant) -> None:
    """Show every field of a participant's row, to help judge a duplicate."""
    fields = [
        ("Full Name", p.name),
        ("E-mail", p.email),
        ("Organization", p.organization),
        ("Position", p.position),
        ("Specific topic", p.topic),
        ("Keywords", p.keywords),
        ("Who they want to meet", p.who_to_meet),
        ("Objectives", "; ".join(p.objectives)),
        ("Chosen themes", "; ".join(p.groups)),
        ("Multidisciplinary preference", p.multidisciplinary),
        ("Comments", p.comments),
    ]
    for label, value in fields:
        st.markdown(f"**{label}:** {value or '—'}")


def review_section() -> None:
    """Review step: surface likely duplicates and un-matchable rows, and let the
    organizer exclude ANY applicant. Nothing is ever deleted — exclusions only
    feed `excluded_ids`, which generation skips."""
    participants: list[Participant] | None = st.session_state.get("participants")
    if not participants:
        return

    excluded: set[int] = st.session_state["excluded_ids"]
    clusters = find_duplicates(participants)
    unusable = find_unusable(participants)
    unusable_reason = {p.id: reason for p, reason in unusable}
    shown: set[int] = set()  # each applicant gets exactly one checkbox (unique key)

    st.subheader("2. Review applicants")
    st.caption(
        "Flagged rows need a look before matching. Uncheck anyone you do NOT want "
        "included — nothing is ever deleted, they are just skipped."
    )

    def keep_checkbox(p: Participant, default: bool, note: str = "") -> None:
        key = f"rev_{p.id}"
        if key not in st.session_state:
            st.session_state[key] = default
        c1, c2 = st.columns([3, 2])
        label = f"{p.name} · {p.email or '—'} · {p.organization or '—'}"
        if note:
            label += f"  —  _{note}_"
        keep = c1.checkbox(label, key=key)
        with c2.popover("Full submission", icon=":material/info:"):
            _render_full_submission(p)
        if keep:
            excluded.discard(p.id)
        else:
            excluded.add(p.id)
        shown.add(p.id)

    if clusters:
        st.markdown("**Possible duplicates** — we suggest keeping the most recent of each.")
        for cluster in clusters:
            st.markdown(f"_{cluster.reason}:_")
            for p in cluster.members:
                keep_checkbox(
                    p,
                    default=p.id == cluster.suggested_keep_id,
                    note=unusable_reason.get(p.id, ""),
                )

    unusable_only = [(p, r) for p, r in unusable if p.id not in shown]
    if unusable_only:
        st.markdown(
            "**Hard to match** — missing info, so the AI has little to go on. "
            "Keep them only if you can place them by hand later."
        )
        for p, reason in unusable_only:
            keep_checkbox(p, default=True, note=reason)

    with st.expander("Exclude anyone else"):
        st.caption("Uncheck to leave someone out of the matching (e.g. a withdrawal).")
        for p in participants:
            if p.id not in shown:
                keep_checkbox(p, default=True)

    st.session_state["excluded_ids"] = excluded
    n_excluded = len(excluded)
    if n_excluded:
        st.info(f"{n_excluded} row(s) will be excluded from matching.")


def split_locked(
    groups: list[Group], active: list[Participant], min_size: int
) -> tuple[list[Group], list[int], bool]:
    """Split current circles for a lock-aware regeneration.

    Returns (locked_groups kept as-is, ids of active applicants free to regroup,
    too_few) where too_few is True when the unlocked pool is below min_size.
    """
    locked_groups = [g for g in groups if g.locked]
    locked_ids = {pid for g in locked_groups for pid in g.member_ids}
    unlocked_pool_ids = [p.id for p in active if p.id not in locked_ids]
    too_few = len(unlocked_pool_ids) < min_size
    return locked_groups, unlocked_pool_ids, too_few


def prune_locked(locked_groups: list[Group], active_ids: set[int]) -> list[Group]:
    """Drop no-longer-active members from locked circles, then drop any circle
    left empty. An applicant who is both excluded and in a locked circle must
    not survive into the export. Dropping a member may push a locked circle
    below min_size — that is accepted (organizer-owned); we don't re-balance."""
    kept: list[Group] = []
    for g in locked_groups:
        g.member_ids = [i for i in g.member_ids if i in active_ids]
        if g.member_ids:
            kept.append(g)
    return kept


def _clear_editor_widget_state() -> None:
    """Drop index-keyed editor widget state so the editor re-seeds from the new
    circles after a (re)generation — otherwise stale lock/name/move state would
    bleed onto the freshly created circles."""
    for key in [k for k in st.session_state if k.startswith(("lock_", "name_", "assign_"))]:
        del st.session_state[key]


_EXPECTED_SECONDS = 100  # rough time a full-cohort match takes; drives the progress bar


def compute_match(
    settings: dict, pool: list[Participant], participants: list[Participant],
    pool_ids: list[int], api_key: str,
) -> tuple[list[Group], list[str]]:
    """The slow work: ask the model, then balance. Pure (no Streamlit calls) so it
    can run safely in a background thread."""
    client = LLMClient(settings["model"], api_key)
    raw = generate_groups(
        client, pool, settings["weights"], settings["ideal"],
        settings["min_size"], settings["max_size"], THEMES,
    )
    return balance_groups(
        raw, participants, pool_ids,
        settings["min_size"], settings["max_size"], settings["ideal"],
    )


def _match_worker(holder: dict, settings, pool, participants, pool_ids, api_key) -> None:
    """Background thread: run the match and record the outcome in `holder`. Data
    fields are written before `status`, so a reader that sees status set always
    sees a complete result."""
    try:
        groups, notes = compute_match(settings, pool, participants, pool_ids, api_key)
        holder["groups"], holder["notes"] = groups, notes
        holder["status"] = "done"
    except LLMError as e:
        holder["error"] = str(e)
        holder["status"] = "error"
    except Exception as e:  # noqa: BLE001 - never let the thread hang the UI
        holder["error"] = f"Unexpected error: {e}"
        holder["status"] = "error"


def _start_generation(settings: dict, participants: list[Participant], active: list[Participant]) -> None:
    """Validate synchronously, then kick off the match in a background thread and
    switch the UI into its locked 'matching…' state."""
    current: list[Group] = st.session_state.get("groups") or []
    locked_groups, pool_ids, too_few = split_locked(current, active, settings["min_size"])

    # Locks that leave too few free applicants: skip rather than crash/drop anyone.
    if locked_groups and too_few:
        st.warning(
            f"Only {len(pool_ids)} applicant(s) are free to regroup after locking — "
            f"fewer than the {settings['min_size']}-member minimum. Unlock a circle "
            "or exclude fewer people, then try again. Nothing was regenerated."
        )
        return

    pool_set = set(pool_ids)
    pool = [p for p in active if p.id in pool_set]
    if not pool:
        st.warning("No applicants to match — everyone is excluded. Re-include someone in step 2.")
        return

    api_key = get_secret("ANTHROPIC_API_KEY")
    try:
        LLMClient(settings["model"], api_key)  # fail fast on a missing/empty key
    except LLMError as e:
        st.error(str(e))
        return

    holder: dict = {"status": "running"}
    threading.Thread(
        target=_match_worker,
        args=(holder, settings, pool, participants, pool_ids, api_key),
        daemon=True,
    ).start()
    st.session_state["gen"] = {
        "holder": holder,
        "start": time.monotonic(),
        "n": len(pool),
        "locked_groups": locked_groups,
        "active_ids": {p.id for p in active},
    }
    st.rerun()


def _progress_fraction(elapsed: float, expected: float = _EXPECTED_SECONDS) -> float:
    """Time-based estimate for the progress bar — eases toward 0.95 but never
    reaches 1.0 until the job actually finishes (a single model call gives no true
    progress signal, so this is reassurance, not telemetry)."""
    if expected <= 0:
        return 0.0
    return min(0.95, elapsed / expected)


def _finalize_generation() -> None:
    """Move a just-finished background match into place (run once per rerun, before
    the normal view renders)."""
    res = st.session_state.pop("gen_result", None)
    if not res:
        return
    meta = st.session_state.pop("gen_meta", {})
    if res.get("status") == "error":
        st.session_state["gen_error"] = res.get("error", "Matching failed.")
        return
    # Re-attach locked circles, dropping members now excluded and any emptied circle.
    locked_groups = prune_locked(meta.get("locked_groups", []), meta.get("active_ids", set()))
    st.session_state["groups"] = locked_groups + res["groups"]
    _clear_editor_widget_state()
    st.session_state["gen_notes"] = res.get("notes", [])
    st.session_state["gen_kept_locked"] = len(locked_groups)


def render_generating_view() -> None:
    """Locked 'matching…' screen: a time-based progress bar and a Cancel button,
    nothing else interactive, so a stray click can't disrupt the run."""
    gen = st.session_state["gen"]
    st.subheader("Matching applicants into circles…")
    st.caption(
        f"Grouping {gen['n']} applicants with AI. This usually takes a minute or two — "
        "please keep this tab open."
    )

    @st.fragment(run_every=1.0)
    def _poll() -> None:
        gen = st.session_state.get("gen")
        if not gen:
            return
        if gen["holder"].get("status") in ("done", "error"):
            st.session_state["gen_result"] = gen["holder"]
            st.session_state["gen_meta"] = {
                "locked_groups": gen["locked_groups"], "active_ids": gen["active_ids"],
            }
            del st.session_state["gen"]
            st.rerun(scope="app")
            return
        elapsed = time.monotonic() - gen["start"]
        st.progress(_progress_fraction(elapsed), text=f"Working… ~{int(elapsed)}s elapsed")

    _poll()

    if st.button("Cancel", icon=":material/close:"):
        del st.session_state["gen"]  # abandon the result; the call finishes server-side
        st.session_state["gen_cancelled"] = True
        st.rerun()


def _show_generation_result() -> None:
    """Show the one-time notes/success from a finished match, next to the circles."""
    if "gen_notes" not in st.session_state:
        return
    kept = st.session_state.pop("gen_kept_locked", 0)
    notes = st.session_state.pop("gen_notes")
    if kept:
        st.caption(f"Kept {kept} locked circle(s) unchanged.")
    for note in notes:
        st.info(note)
    groups = st.session_state.get("groups") or []
    st.success(f"Created {len([g for g in groups if g.member_ids])} circles.")


def generate_section(settings: dict) -> None:
    participants: list[Participant] | None = st.session_state.get("participants")
    if not participants:
        return

    excluded: set[int] = st.session_state["excluded_ids"]
    active = [p for p in participants if p.id not in excluded]

    if st.session_state.get("groups"):
        # Circles already exist — keep generation out of the way so the editor is
        # the main view, and make clear it is a fresh rebuild, not an edit.
        with st.expander(":material/refresh: Re-generate circles from scratch"):
            st.caption(
                f"Rebuilds circles from the {len(active)} applicants and **discards "
                "your manual edits**. Manual edits below never feed back into this."
            )
            if st.button("Re-generate", type="secondary", icon=":material/refresh:"):
                _start_generation(settings, participants, active)
    else:
        st.subheader("3. Generate circles")
        st.caption(
            f"{len(active)} applicants will be matched. The AI proposes the circles; "
            "group sizes and the no-same-organization rule are then enforced automatically."
        )
        if st.button("Generate circles", type="primary", icon=":material/auto_awesome:"):
            _start_generation(settings, participants, active)


def _apply_moves() -> None:
    """Callback: rebuild membership from the per-person circle selectors."""
    groups = st.session_state.get("groups") or []
    assignment: dict[int, int] = {}
    for gi, g in enumerate(groups):
        for pid in g.member_ids:
            assignment[pid] = st.session_state.get(f"assign_{pid}", gi)
    for g in groups:
        g.member_ids = []
    for pid, gi in assignment.items():
        if 0 <= gi < len(groups):
            groups[gi].member_ids.append(pid)
    st.session_state["groups"] = [g for g in groups if g.member_ids]


def edit_section(settings: dict) -> None:
    groups: list[Group] | None = st.session_state.get("groups")
    participants: list[Participant] | None = st.session_state.get("participants")
    if not groups or not participants:
        return

    by_id = {p.id: p for p in participants}
    st.subheader("4. Review & edit circles")
    st.caption(
        "These edits are yours alone — they are never used to re-generate. "
        "Add an empty circle, move people into it via their circle selector, then "
        "click **Apply moves**. Rename a circle in its title box. Sizes outside 3–6 are flagged. "
        "Happy with a circle? **Lock** it so a re-generate leaves it untouched and only "
        "reshuffles the rest."
    )

    # Add-circle form: submitting (Enter or button) creates an empty named circle.
    with st.form("add_circle_form", clear_on_submit=True):
        ac1, ac2 = st.columns([3, 1])
        new_name = ac1.text_input(
            "New circle name", placeholder="e.g. Cancer Genomics", label_visibility="collapsed"
        )
        submitted = ac2.form_submit_button(
            "Add circle", use_container_width=True, icon=":material/add:"
        )
    if submitted and new_name.strip():
        groups.append(Group(name=new_name.strip(), member_ids=[]))
        st.session_state["groups"] = groups

    group_names = [g.name for g in groups]

    for gi, g in enumerate(groups):
        size = len(g.member_ids)
        ok = settings["min_size"] <= size <= settings["max_size"]
        flag = "" if ok else " :material/warning:"
        lock_mark = " :material/lock:" if g.locked else ""
        with st.expander(f"{g.name} — {size} members{flag}{lock_mark}", expanded=not ok):
            g.name = st.text_input("Circle name", g.name, key=f"name_{gi}")
            g.locked = st.checkbox(
                "Lock this circle", value=g.locked, key=f"lock_{gi}",
                help="Keep this circle exactly as-is when you re-generate; only the "
                "unlocked applicants are reshuffled.",
            )
            if g.rationale:
                st.caption(g.rationale)
            for fl in g.flags:
                st.warning(fl)
            for pid in list(g.member_ids):
                p = by_id[pid]
                c1, c2 = st.columns([3, 2])
                c1.markdown(f"**{p.name}** — {p.position or '—'}, {p.organization or '—'}")
                c2.selectbox(
                    "Circle",
                    options=list(range(len(groups))),
                    format_func=lambda i: group_names[i],
                    index=gi,
                    key=f"assign_{pid}",
                    label_visibility="collapsed",
                )

    st.button("Apply moves", on_click=_apply_moves, type="primary", icon=":material/done_all:")


def export_section() -> None:
    groups: list[Group] | None = st.session_state.get("groups")
    participants = st.session_state.get("participants")
    if not groups or not participants:
        return
    st.subheader("5. Export")
    st.caption(
        "Download the circles as an Excel workbook (one sheet listing every circle "
        "and its members) to share with the team."
    )
    xlsx = build_xlsx(groups, participants)
    st.download_button(
        "Download grouped XLSX",
        data=xlsx,
        icon=":material/download:",
        file_name="peer_mentoring_circles.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


_BRAND_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Spectral:wght@500;600&display=swap');
/* Headings only — do NOT restyle the body font globally, or Streamlit's
   Material icon ligatures (rendered in a special icon font) break. */
h1, h2, h3, [data-testid="stHeading"] { font-family: 'Spectral', Georgia, serif; letter-spacing: -0.02em; color: #2b2824; }
::selection { background: #E6122C; color: #fff; }
.pm-head { display: flex; align-items: center; gap: 16px; padding: 4px 0 2px; }
.pm-head h1 { font-family: 'Spectral', serif; font-weight: 600; font-size: 30px; margin: 0; line-height: 1.1; color: #2b2824; }
.pm-head p { margin: 4px 0 0; color: #767676; font-size: 15px; }
.pm-rail { display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0 6px; }
.pm-step { display: flex; align-items: center; gap: 8px; padding: 7px 13px 7px 8px; border: 1px solid #E2E2E2; border-radius: 999px; font-size: 13px; color: #767676; font-weight: 500; background: #fff; }
.pm-step .n { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; background: #E8E8E8; color: #767676; font-weight: 600; }
.pm-step.on { border-color: #E6122C; color: #C31313; background: #FDEAEC; }
.pm-step.on .n { background: #E6122C; color: #fff; }
.pm-step.done .n { background: #C31313; color: #fff; }
</style>
"""

_HEADER_HTML = """
<div class="pm-head">
  <svg width="44" height="44" viewBox="0 0 46 46" fill="none" aria-hidden="true">
    <circle cx="23" cy="23" r="21.5" stroke="#E6122C" stroke-width="1.4" opacity=".26"/>
    <circle cx="23" cy="23" r="14.5" stroke="#E6122C" stroke-width="1.4" opacity=".5"/>
    <circle cx="18" cy="23" r="4.1" fill="#E6122C"/>
    <circle cx="28" cy="18.5" r="4.1" fill="#E6122C" opacity=".72"/>
    <circle cx="28" cy="27.5" r="4.1" fill="#E6122C" opacity=".46"/>
  </svg>
  <div>
    <h1>Peer Mentoring Circles</h1>
    <p>Sort applicants into circles of 3&ndash;6 who share goals and complementary expertise.</p>
  </div>
</div>
"""

_STEPS = ["Upload", "Review", "Generate", "Edit", "Export"]


def render_rail(current: int | None = None) -> None:
    """A thin progress rail reflecting where the organizer is in the flow."""
    if current is None:
        has_people = st.session_state.get("participants") is not None
        has_groups = bool(st.session_state.get("groups"))
        current = 3 if has_groups else (1 if has_people else 0)  # index into _STEPS
    cells = []
    for i, label in enumerate(_STEPS):
        cls = "on" if i == current else ("done" if i < current else "")
        mark = "&#10003;" if i < current else str(i + 1)
        cells.append(f'<div class="pm-step {cls}"><span class="n">{mark}</span>{label}</div>')
    st.markdown(f'<div class="pm-rail">{"".join(cells)}</div>', unsafe_allow_html=True)


def main():
    st.set_page_config(
        page_title="IMFAHE Peer Mentoring Matching", page_icon=":material/hub:", layout="wide"
    )
    cfg = load_config()
    st.markdown(_BRAND_CSS, unsafe_allow_html=True)  # before the gate, so login is branded too
    if not check_password():
        return
    init_state(cfg)

    st.markdown(_HEADER_HTML, unsafe_allow_html=True)
    _finalize_generation()

    if st.session_state.get("gen"):
        # Locked 'matching…' view — the sidebar and all steps are withheld so a
        # stray click can't disrupt the run.
        render_rail(current=2)
        render_generating_view()
        return

    settings = sidebar_controls(cfg)
    render_rail()

    if "gen_error" in st.session_state:
        st.error(st.session_state.pop("gen_error"))
    if st.session_state.pop("gen_cancelled", False):
        st.info("Matching cancelled — nothing was changed.")

    upload_section()
    review_section()
    generate_section(settings)
    _show_generation_result()
    edit_section(settings)
    export_section()


if __name__ == "__main__":
    main()
