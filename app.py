"""IMFAHE Peer Mentoring Circles — matching app.

Flow (one step at a time, rail-navigated): log in -> upload the form export ->
review & clean -> generate circles with an LLM -> edit (move/rename/lock) ->
finalize (download a grouped XLSX).
"""
from __future__ import annotations

import html
import math
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

_STEPS = ["Upload", "Review", "Generate", "Edit", "Finalize"]


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


# --------------------------------------------------------------------------- mark + login


_MARK_SVG = """
<svg width="44" height="44" viewBox="0 0 46 46" fill="none" aria-hidden="true">
  <circle cx="23" cy="23" r="21.5" stroke="#E6122C" stroke-width="1.4" opacity=".26"/>
  <circle cx="23" cy="23" r="14.5" stroke="#E6122C" stroke-width="1.4" opacity=".5"/>
  <circle cx="18" cy="23" r="4.1" fill="#E6122C"/>
  <circle cx="28" cy="18.5" r="4.1" fill="#E6122C" opacity=".72"/>
  <circle cx="28" cy="27.5" r="4.1" fill="#E6122C" opacity=".46"/>
</svg>
"""

_LOGIN_HTML = """
<div class="pm-login">
  <span class="pm-lmark"><svg width="78" height="78" viewBox="0 0 46 46" fill="none" aria-hidden="true">
    <circle class="pm-ring" cx="23" cy="23" r="21.5" stroke="#E6122C" stroke-width="1.3"/>
    <circle class="pm-ring pm-ring2" cx="23" cy="23" r="14.5" stroke="#E6122C" stroke-width="1.3"/>
    <circle cx="23" cy="23" r="3.3" fill="#E6122C"/>
    <g class="pm-orbit">
      <circle cx="23" cy="8.5" r="3.6" fill="#E6122C"/>
      <circle cx="35.5" cy="29.5" r="3.6" fill="#E6122C" opacity=".72"/>
      <circle cx="10.5" cy="29.5" r="3.6" fill="#E6122C" opacity=".48"/>
    </g>
  </svg></span>
  <h1>IMFAHE Peer Mentoring Circles</h1>
  <p>Circles that publish, pitch, and mentor together.</p>
</div>
"""

_HEADER_HTML = f"""
<div class="pm-head">
  {_MARK_SVG}
  <div>
    <h1>Peer Mentoring Circles</h1>
    <p>Circles that publish, pitch, and mentor together.</p>
  </div>
</div>
"""


def check_password() -> bool:
    """Single shared 'magic word' gate. Set APP_PASSWORD in secrets/env."""
    expected = get_secret("APP_PASSWORD")
    if not expected:  # no password configured -> open access (local dev)
        return True
    if st.session_state.get("authed"):
        return True
    st.markdown(_LOGIN_HTML, unsafe_allow_html=True)
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
    st.session_state.setdefault("step", 0)


# --------------------------------------------------------------------------- sidebar


def sidebar_controls(cfg: dict) -> dict:
    st.sidebar.header("Matching settings")

    model = cfg["provider"]["model"]

    st.sidebar.subheader(
        "Group size",
        help="Target sizes for each circle. People from the same organization are "
        "kept in different circles whenever sizes allow.",
    )
    m = cfg["matching"]
    col1, col2, col3 = st.sidebar.columns(3)
    min_size = col1.number_input("Min", 2, 10, int(m["min_group_size"]))
    ideal = col2.number_input("Ideal", 2, 10, int(m["ideal_group_size"]))
    max_size = col3.number_input("Max", 2, 12, int(m["max_group_size"]))

    st.sidebar.subheader(
        "Question weights (0–5)",
        help="Higher weight = that answer counts for more when forming circles.",
    )
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


# --------------------------------------------------------------------------- step 1: upload


def upload_step() -> None:
    st.subheader("Upload the responses")
    st.caption("Upload the form responses (CSV or XLSX).")
    file = st.file_uploader("Responses file", type=["csv", "xlsx", "xls"], label_visibility="collapsed")
    if file is not None:
        # The uploader returns the same file on every rerun. Only (re)parse when a
        # genuinely different file is uploaded — otherwise we'd wipe the circles.
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

    participants = st.session_state.get("participants")
    if participants:
        st.success(f"Loaded {len(participants)} applicants.")
        with st.expander(f"Preview {len(participants)} applicants", expanded=False):
            st.dataframe(
                [
                    {"Name": p.name, "Org": p.organization,
                     "Themes": ", ".join(p.groups), "Topic": p.topic}
                    for p in participants
                ],
                use_container_width=True,
                hide_index=True,
            )
        if st.button("Continue to Review →", type="primary"):
            _goto(1)


# --------------------------------------------------------------------------- step 2: review


def _render_full_submission(p: Participant) -> None:
    """Show every field of a participant's row, to help judge a duplicate."""
    fields = [
        ("Full Name", p.name), ("E-mail", p.email), ("Organization", p.organization),
        ("Position", p.position), ("Specific topic", p.topic), ("Keywords", p.keywords),
        ("Who they want to meet", p.who_to_meet), ("Objectives", "; ".join(p.objectives)),
        ("Chosen themes", "; ".join(p.groups)),
        ("Multidisciplinary preference", p.multidisciplinary), ("Comments", p.comments),
    ]
    for label, value in fields:
        st.markdown(f"**{label}:** {value or '—'}")


def review_step() -> None:
    """Review step: surface likely duplicates and un-matchable rows, and let the
    organizer exclude ANY applicant. Nothing is ever deleted — exclusions only feed
    `excluded_ids`, which generation skips."""
    participants: list[Participant] = st.session_state["participants"]
    excluded: set[int] = st.session_state["excluded_ids"]
    clusters = find_duplicates(participants)
    unusable = find_unusable(participants)
    unusable_reason = {p.id: reason for p, reason in unusable}
    shown: set[int] = set()

    st.subheader("Review applicants")
    st.caption(
        f"We flagged **{len(clusters)} possible duplicate(s)** (keeping the latest of each) "
        f"and **{len(unusable)} incomplete row(s)**. Uncheck anyone to leave them out — "
        "nothing is deleted."
    )

    def keep_checkbox(p: Participant, default: bool, note: str = "", details: bool = False) -> None:
        key = f"rev_{p.id}"
        if key not in st.session_state:
            st.session_state[key] = default
        label = f"**{p.name}** · {p.email or '—'}"
        if note:
            label += f" · _{note}_"
        if details:  # inspect an arbitrary applicant before excluding them
            c1, c2 = st.columns([3, 1])
            keep = c1.checkbox(label, key=key)
            with c2.popover("Details", icon=":material/info:"):
                _render_full_submission(p)
        else:  # compact single line for the flagged rows
            keep = st.checkbox(label, key=key)
        if keep:
            excluded.discard(p.id)
        else:
            excluded.add(p.id)
        shown.add(p.id)

    if clusters:
        for cluster in clusters:
            for p in cluster.members:
                kept = p.id == cluster.suggested_keep_id
                note = unusable_reason.get(p.id) or ("latest · kept" if kept else "earlier duplicate")
                keep_checkbox(p, default=kept, note=note)

    unusable_only = [(p, r) for p, r in unusable if p.id not in shown]
    for p, reason in unusable_only:
        keep_checkbox(p, default=True, note=reason)

    with st.expander("See all applicants & exclude anyone else"):
        for p in participants:
            if p.id not in shown:
                keep_checkbox(p, default=True, details=True)

    st.session_state["excluded_ids"] = excluded
    active = len(participants) - len(excluded)
    st.caption(f"**{active} applicants** will be matched.")
    if st.button("Continue to Generate →", type="primary"):
        _goto(2)


# --------------------------------------------------------------------------- balancing helpers


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
    """Drop no-longer-active members from locked circles, then drop any circle left
    empty. An applicant who is both excluded and in a locked circle must not survive
    into the export. Dropping a member may push a locked circle below min_size — that
    is accepted (organizer-owned); we don't re-balance."""
    kept: list[Group] = []
    for g in locked_groups:
        g.member_ids = [i for i in g.member_ids if i in active_ids]
        if g.member_ids:
            kept.append(g)
    return kept


def _clear_editor_widget_state() -> None:
    """Drop index-keyed editor widget state so the editor re-seeds from the new
    circles after a (re)generation."""
    for key in [k for k in st.session_state if k.startswith(("lock_", "name_", "assign_"))]:
        del st.session_state[key]


# --------------------------------------------------------------------------- step 3: generate (async)


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
    fields are written before `status`, so a reader that sees status set always sees
    a complete result."""
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

    if locked_groups and too_few:
        st.warning(
            f"Only {len(pool_ids)} applicant(s) are free to regroup after locking — "
            f"fewer than the {settings['min_size']}-member minimum. Unlock a circle or "
            "exclude fewer people, then try again. Nothing was regenerated."
        )
        return

    pool_set = set(pool_ids)
    pool = [p for p in active if p.id in pool_set]
    if not pool:
        st.warning("No applicants to match — everyone is excluded. Re-include someone first.")
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
        "holder": holder, "start": time.monotonic(), "n": len(pool),
        "locked_groups": locked_groups, "active_ids": {p.id for p in active},
    }
    st.rerun()


def _progress_fraction(elapsed: float, expected: float = _EXPECTED_SECONDS) -> float:
    """Time-based estimate for the progress bar — eases toward 0.95 but never reaches
    1.0 until the job actually finishes (a single model call gives no true progress
    signal, so this is reassurance, not telemetry)."""
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
    locked_groups = prune_locked(meta.get("locked_groups", []), meta.get("active_ids", set()))
    st.session_state["groups"] = locked_groups + res["groups"]
    _clear_editor_widget_state()
    st.session_state["gen_notes"] = res.get("notes", [])
    st.session_state["gen_kept_locked"] = len(locked_groups)
    st.session_state["step"] = 3  # land on Edit to show the circles


def render_generating_view() -> None:
    """Locked 'matching…' screen: a time-based progress bar and a Cancel button,
    nothing else interactive, so a stray click can't disrupt the run."""
    gen = st.session_state["gen"]
    st.subheader("Matching applicants into circles…")
    st.caption(
        f"Grouping {gen['n']} applicants with AI. This usually takes a few minutes. "
        "Please keep this tab open."
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


def _active(participants: list[Participant]) -> list[Participant]:
    excluded: set[int] = st.session_state["excluded_ids"]
    return [p for p in participants if p.id not in excluded]


def generate_step(settings: dict) -> None:
    participants: list[Participant] = st.session_state["participants"]
    active = _active(participants)
    st.subheader("Generate circles")
    st.caption(
        f"{len(active)} applicants ready. The AI proposes the circles; sizes and the "
        "no-same-organization rule are enforced automatically."
    )
    if st.session_state.get("groups"):
        st.info(
            "You already have circles — generating again rebuilds them from scratch "
            "(manual edits discarded; locked circles kept)."
        )
    if st.button("Generate circles", type="primary", icon=":material/auto_awesome:"):
        _start_generation(settings, participants, active)


# --------------------------------------------------------------------------- step 4: edit


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


def _show_generation_result() -> None:
    """Show the one-time notes/success from a finished match, below the title."""
    if "gen_notes" not in st.session_state:
        return
    kept = st.session_state.pop("gen_kept_locked", 0)
    notes = st.session_state.pop("gen_notes")
    groups = st.session_state.get("groups") or []
    st.success(f"Created {len([g for g in groups if g.member_ids])} circles.")
    if kept:
        st.caption(f"Kept {kept} locked circle(s) unchanged.")
    for note in notes:
        st.info(note)


def edit_step(settings: dict) -> None:
    groups: list[Group] = st.session_state["groups"]
    participants: list[Participant] = st.session_state["participants"]
    by_id = {p.id: p for p in participants}

    st.subheader("Review & edit circles")
    st.caption(
        "Move people between circles, rename them, or **lock** the ones you're happy "
        "with. Sizes outside 3–6 are flagged."
    )
    _show_generation_result()

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
                    "Circle", options=list(range(len(groups))),
                    format_func=lambda i: group_names[i], index=gi,
                    key=f"assign_{pid}", label_visibility="collapsed",
                )

    st.divider()
    with st.form("add_circle_form", clear_on_submit=True):
        ac1, ac2 = st.columns([3, 1])
        new_name = ac1.text_input(
            "New circle name", placeholder="e.g. Cancer Genomics", label_visibility="collapsed"
        )
        submitted = ac2.form_submit_button("Add circle", use_container_width=True, icon=":material/add:")
    if submitted and new_name.strip():
        groups.append(Group(name=new_name.strip(), member_ids=[]))
        st.session_state["groups"] = groups

    b1, b2, b3 = st.columns(3)
    b1.button("Apply moves", on_click=_apply_moves, type="primary",
              icon=":material/done_all:", use_container_width=True)
    regen = b2.button("Re-generate", icon=":material/refresh:", use_container_width=True)
    cont = b3.button("Continue to Finalize →", use_container_width=True)
    if regen:
        _start_generation(settings, participants, _active(participants))
    if cont:
        _goto(4)


# --------------------------------------------------------------------------- step 5: finalize


def _circles_viz_html(groups: list[Group], by_id: dict[int, Participant]) -> str:
    """At-a-glance grid: each circle a hub-and-dots card that opens in place to list
    its members. Native <details> — no Streamlit round-trip, so opening one is instant."""
    cards = []
    for g in groups:
        n = len(g.member_ids)
        dots = []
        for k in range(n):
            ang = -math.pi / 2 + 2 * math.pi * k / max(n, 1)
            x, y = round(26 * math.cos(ang)), round(26 * math.sin(ang))
            dots.append(f'<span class="pm-cdot" style="transform:translate({x}px,{y}px)"></span>')
        members = "".join(
            f'<li><b>{html.escape(p.name)}</b>'
            f'<span>{html.escape(p.position or "—")} · {html.escape(p.organization or "—")}</span></li>'
            for p in (by_id[pid] for pid in g.member_ids)
        )
        rationale = f'<p class="pm-rat">{html.escape(g.rationale)}</p>' if g.rationale else ""
        cards.append(
            f'<details class="pm-gcard"><summary>'
            f'<div class="pm-cluster"><span class="pm-chub"></span>{"".join(dots)}</div>'
            f'<div class="pm-gname">{html.escape(g.name)}</div>'
            f'<div class="pm-gn">{n} members</div></summary>'
            f'{rationale}<ul class="pm-mlist">{members}</ul></details>'
        )
    return f'<div class="pm-grid">{"".join(cards)}</div>'


def finalize_step() -> None:
    participants: list[Participant] = st.session_state["participants"]
    all_groups: list[Group] = st.session_state["groups"]
    groups = [g for g in all_groups if g.member_ids]
    by_id = {p.id: p for p in participants}
    n_people = sum(len(g.member_ids) for g in groups)

    st.subheader("Finalize")
    st.caption(
        f"**{len(groups)} circles, {n_people} people.** Download the circles as an Excel "
        "workbook (one sheet listing every circle and its members)."
    )
    xlsx = build_xlsx(all_groups, participants)
    st.download_button(
        "Download grouped XLSX", data=xlsx, type="primary", icon=":material/download:",
        file_name="peer_mentoring_circles.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.caption("Every circle at a glance — click one to see its members.")
    st.html(_circles_viz_html(groups, by_id))


# --------------------------------------------------------------------------- navigation + rail


def _reachable(participants, groups) -> set[int]:
    s = {0}
    if participants is not None:
        s |= {1, 2}
    if groups:
        s |= {3, 4}
    return s


def _goto(step: int) -> None:
    st.session_state["step"] = step
    st.rerun()


def _resolve_step(participants, groups) -> int:
    """Current step: the stored step, clamped to what's reachable given the data on
    hand (e.g. a new upload clears groups, so step 3/4 become unreachable)."""
    reachable = _reachable(participants, groups)
    step = st.session_state.get("step", 0)
    if step not in reachable:
        step = 3 if groups else (1 if participants is not None else 0)
        st.session_state["step"] = step
    return step


def _nav_to(step: int) -> None:
    st.session_state["step"] = step


def render_rail(current: int, participants=None, groups=None, clickable: bool = True) -> None:
    """Step rail as real buttons — they rerun in-place (preserving session_state),
    unlike anchor links which full-reload and reset the session."""
    reachable = _reachable(participants, groups) if clickable else set()
    cols = st.columns(len(_STEPS))
    for i, label in enumerate(_STEPS):
        is_current = i == current
        enabled = clickable and (is_current or i in reachable)
        mark = "✓" if i < current else str(i + 1)
        cols[i].button(
            f"{mark}  {label}",
            key=f"rail_{i}",
            type="primary" if is_current else "secondary",
            use_container_width=True,
            disabled=not enabled,
            on_click=_nav_to if enabled else None,
            args=(i,) if enabled else None,
        )


# --------------------------------------------------------------------------- theme / CSS


_BRAND_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Spectral:wght@500;600&display=swap');
/* Headings only — do NOT restyle the body font globally, or Streamlit's Material
   icon ligatures (rendered in a special icon font) break. */
h1, h2, h3, [data-testid="stHeading"] { font-family: 'Spectral', Georgia, serif; letter-spacing: -0.02em; color: #2b2824; }
::selection { background: #E6122C; color: #fff; }
/* dark help tooltips (Streamlit's default light bubble blends into the sidebar) */
[data-testid="stTooltipContent"] { background: #2b2824 !important; border-radius: 8px; box-shadow: 0 8px 24px -8px rgba(0,0,0,.35); }
[data-testid="stTooltipContent"], [data-testid="stTooltipContent"] * { color: #fff !important; font-size: 12.5px; }
.pm-head { display: flex; align-items: center; gap: 16px; padding: 4px 0 2px; }
.pm-head h1 { font-family: 'Spectral', serif; font-weight: 600; font-size: 30px; margin: 0; line-height: 1.1; color: #2b2824; }
.pm-head p { margin: 4px 0 0; color: #767676; font-size: 15px; }
/* step rail */
.pm-rail { display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0 10px; }
.pm-step { display: flex; align-items: center; gap: 8px; padding: 7px 13px 7px 8px; border: 1px solid #E2E2E2; border-radius: 999px; font-size: 13px; color: #767676; font-weight: 500; background: #fff; text-decoration: none; }
a.pm-step { cursor: pointer; }
a.pm-step:hover { border-color: #E6122C; color: #C31313; }
.pm-step.dim { opacity: .45; }
.pm-step .n { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; background: #E8E8E8; color: #767676; font-weight: 600; }
.pm-step.on { border-color: #E6122C; color: #C31313; background: #FDEAEC; }
.pm-step.on .n { background: #E6122C; color: #fff; }
.pm-step.done .n { background: #C31313; color: #fff; }
/* animated login */
.pm-login { display: flex; flex-direction: column; align-items: center; text-align: center; padding: 30px 0 18px; }
.pm-login h1 { font-size: 30px; font-weight: 600; margin: 18px 0 0; animation: pmFade .7s .1s ease-out both; }
.pm-login p { color: #767676; font-size: 14px; margin: 7px 0 0; animation: pmFade .7s .2s ease-out both; }
.pm-lmark { animation: pmFade .7s ease-out both; }
.pm-ring { transform-box: fill-box; transform-origin: center; animation: pmBreathe 3.2s ease-in-out infinite; }
.pm-ring2 { animation-delay: .6s; }
.pm-orbit { transform-box: fill-box; transform-origin: center; animation: pmSpin 14s linear infinite; }
@keyframes pmBreathe { 0%,100% { opacity: .22 } 50% { opacity: .62 } }
@keyframes pmSpin { to { transform: rotate(360deg) } }
@keyframes pmFade { from { opacity: 0; transform: translateY(8px) } to { opacity: 1; transform: none } }
@media (prefers-reduced-motion: reduce) { .pm-ring, .pm-orbit, .pm-lmark, .pm-login h1, .pm-login p { animation: none !important; } }
/* finalize circle grid */
.pm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 12px; margin: 4px 0 18px; }
.pm-gcard { border: 1px solid #E2E2E2; border-radius: 12px; background: #fff; padding: 14px; text-align: center; }
.pm-cluster { position: relative; width: 76px; height: 76px; margin: 0 auto 10px; }
.pm-cdot { position: absolute; width: 14px; height: 14px; border-radius: 50%; background: #E6122C; top: 50%; left: 50%; margin: -7px; }
.pm-chub { position: absolute; width: 20px; height: 20px; border-radius: 50%; background: #FDEAEC; border: 1.5px solid #E6122C; top: 50%; left: 50%; margin: -10px; }
.pm-gname { font-weight: 600; font-size: 12.5px; line-height: 1.3; color: #2b2824; }
.pm-gn { color: #767676; font-size: 11.5px; margin-top: 3px; }
.pm-gcard > summary { list-style: none; cursor: pointer; }
.pm-gcard > summary::-webkit-details-marker { display: none; }
.pm-gcard:hover { border-color: #E6122C; }
.pm-gcard[open] { grid-column: 1 / -1; }
.pm-rat { font-size: 11.5px; color: #767676; font-style: italic; margin: 12px 0 0; text-align: center; }
.pm-mlist { list-style: none; padding: 12px 0 2px; margin: 10px 0 0; border-top: 1px solid #EEE; columns: 2; column-gap: 24px; text-align: left; }
.pm-mlist li { font-size: 12px; padding: 4px 0; line-height: 1.3; break-inside: avoid; }
.pm-mlist li b { color: #2b2824; display: block; }
.pm-mlist li span { color: #767676; }
</style>
"""


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
        # Locked 'matching…' view — sidebar and all steps withheld so a stray click
        # can't disrupt the run.
        render_rail(2, clickable=False)
        render_generating_view()
        return

    settings = sidebar_controls(cfg)
    participants = st.session_state.get("participants")
    groups = st.session_state.get("groups")
    step = _resolve_step(participants, groups)
    render_rail(step, participants, groups)

    if "gen_error" in st.session_state:
        st.error(st.session_state.pop("gen_error"))
    if st.session_state.pop("gen_cancelled", False):
        st.info("Matching cancelled — nothing was changed.")

    if step == 0:
        upload_step()
    elif step == 1:
        review_step()
    elif step == 2:
        generate_step(settings)
    elif step == 3:
        edit_step(settings)
    elif step == 4:
        finalize_step()


if __name__ == "__main__":
    main()
