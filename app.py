"""IMFAHE Peer Mentoring Circles — matching app.

Flow: log in -> upload the 2027 form export -> review parsed applicants ->
tune per-question weights & group sizes -> generate circles with an LLM ->
edit (move people, rename, add/remove circles) -> download a grouped XLSX.
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from matching.balance import balance_groups
from matching.export import build_xlsx
from matching.llm import LLMClient, LLMError
from matching.parser import (
    ParseError,
    Participant,
    find_duplicates,
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
    except Exception:  # secrets file may not exist locally
        pass
    return os.environ.get(key, default)


def check_password() -> bool:
    """Single shared 'magic word' gate. Set APP_PASSWORD in secrets/env."""
    expected = get_secret("APP_PASSWORD")
    if not expected:  # no password configured -> open access (local dev)
        return True
    if st.session_state.get("authed"):
        return True
    st.title("🔒 IMFAHE Peer Mentoring — Matching")
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
    st.sidebar.header("⚙️ Matching settings")

    provider = st.sidebar.selectbox(
        "LLM provider",
        ["openai", "anthropic"],
        index=0 if cfg["provider"]["name"] == "openai" else 1,
    )
    model = cfg["provider"][provider]["model"]
    st.sidebar.caption(f"Model: `{model}` (set in config.toml)")

    st.sidebar.subheader("Group size")
    m = cfg["matching"]
    col1, col2, col3 = st.sidebar.columns(3)
    min_size = col1.number_input("Min", 2, 10, int(m["min_group_size"]))
    ideal = col2.number_input("Ideal", 2, 10, int(m["ideal_group_size"]))
    max_size = col3.number_input("Max", 2, 12, int(m["max_group_size"]))

    st.sidebar.subheader("Question weights (0–5)")
    weights = {}
    for key, label in WEIGHT_FIELDS:
        weights[key] = st.sidebar.slider(
            label, 0, 5, int(st.session_state["weights"].get(key, 0)), key=f"w_{key}"
        )
    st.session_state["weights"] = weights

    return {
        "provider": provider,
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
        try:
            participants, _ = parse_participants(file.getvalue(), file.name)
        except ParseError as e:
            st.error(str(e))
            return
        st.session_state["participants"] = participants
        st.session_state["groups"] = None
        st.session_state["excluded_ids"] = set()
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


def duplicates_section() -> None:
    """Surface likely duplicate submissions and let the organizer exclude rows."""
    participants: list[Participant] | None = st.session_state.get("participants")
    if not participants:
        return
    clusters = find_duplicates(participants)
    if not clusters:
        return

    excluded: set[int] = st.session_state["excluded_ids"]
    st.subheader("2. Review possible duplicates")
    st.caption(
        "These rows look like the same person. Uncheck anyone you do NOT want to "
        "include in the matching. By default we suggest keeping the most recent "
        "submission of each."
    )
    for ci, cluster in enumerate(clusters):
        st.markdown(f"**Possible duplicate — {cluster.reason}:**")
        for p in cluster.members:
            key = f"dup_{p.id}"
            if key not in st.session_state:
                # Default: keep only the suggested (latest) row; exclude the rest.
                st.session_state[key] = p.id == cluster.suggested_keep_id
            c1, c2 = st.columns([3, 2])
            label = f"{p.name} · {p.email} · {p.organization or '—'}"
            keep = c1.checkbox(label, key=key)
            with c2.popover("ℹ️ Full submission"):
                _render_full_submission(p)
            if keep:
                excluded.discard(p.id)
            else:
                excluded.add(p.id)
    st.session_state["excluded_ids"] = excluded
    n_excluded = len(excluded)
    if n_excluded:
        st.info(f"{n_excluded} row(s) will be excluded from matching.")


def _run_generation(settings: dict, participants: list[Participant], active: list[Participant]) -> None:
    api_key = get_secret(
        "OPENAI_API_KEY" if settings["provider"] == "openai" else "ANTHROPIC_API_KEY"
    )
    try:
        client = LLMClient(settings["provider"], settings["model"], api_key)
        with st.spinner("Matching applicants into circles…"):
            raw = generate_groups(
                client, active, settings["weights"], settings["ideal"],
                settings["min_size"], settings["max_size"], THEMES,
            )
            groups, notes = balance_groups(
                raw, participants, [p.id for p in active],
                settings["min_size"], settings["max_size"], settings["ideal"],
            )
    except LLMError as e:
        st.error(str(e))
        return
    st.session_state["groups"] = groups
    for note in notes:
        st.info(note)
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
        with st.expander("↻ Re-generate circles from scratch"):
            st.caption(
                f"Rebuilds circles from the {len(active)} applicants and **discards "
                "your manual edits**. Manual edits below never feed back into this."
            )
            if st.button("Re-generate", type="secondary"):
                _run_generation(settings, participants, active)
    else:
        st.subheader("3. Generate circles")
        st.caption(f"{len(active)} applicants will be matched.")
        if st.button("✨ Generate circles", type="primary"):
            _run_generation(settings, participants, active)


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
        "click **Apply moves**. Rename a circle in its title box. Sizes outside 3–6 are flagged."
    )

    # Add-circle form: submitting (Enter or button) creates an empty named circle.
    with st.form("add_circle_form", clear_on_submit=True):
        ac1, ac2 = st.columns([3, 1])
        new_name = ac1.text_input(
            "New circle name", placeholder="e.g. Cancer Genomics", label_visibility="collapsed"
        )
        submitted = ac2.form_submit_button("➕ Add circle", use_container_width=True)
    if submitted and new_name.strip():
        groups.append(Group(name=new_name.strip(), member_ids=[]))
        st.session_state["groups"] = groups

    group_names = [g.name for g in groups]

    for gi, g in enumerate(groups):
        size = len(g.member_ids)
        ok = settings["min_size"] <= size <= settings["max_size"]
        flag = "" if ok else "  ⚠️"
        with st.expander(f"{g.name} — {size} members{flag}", expanded=not ok):
            g.name = st.text_input("Circle name", g.name, key=f"name_{gi}")
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

    st.button("🔄 Apply moves", on_click=_apply_moves, type="primary")


def export_section() -> None:
    groups: list[Group] | None = st.session_state.get("groups")
    participants = st.session_state.get("participants")
    if not groups or not participants:
        return
    st.subheader("5. Export")
    xlsx = build_xlsx(groups, participants)
    st.download_button(
        "⬇️ Download grouped XLSX",
        data=xlsx,
        file_name="peer_mentoring_circles.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main():
    st.set_page_config(page_title="IMFAHE Peer Mentoring Matching", page_icon="🤝", layout="wide")
    cfg = load_config()
    if not check_password():
        return
    init_state(cfg)

    st.title("🤝 IMFAHE Peer Mentoring Circles — Matching")
    settings = sidebar_controls(cfg)

    upload_section()
    duplicates_section()
    generate_section(settings)
    edit_section(settings)
    export_section()


if __name__ == "__main__":
    main()
