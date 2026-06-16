"""Build the matching prompt and turn the model's JSON into groups."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .llm import LLMClient, parse_json_response
from .parser import Participant


def _norm(text: str) -> str:
    """Lowercase + strip accents, for tolerant name matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def compute_meet_links(participants: list[Participant]) -> dict[int, set[int]]:
    """Map each applicant -> ids of OTHER applicants they named in 'who to meet'.

    Matches a full name (4+ chars) appearing as a substring of the free-text
    field. Conservative on purpose: explicit names only, not fields/keywords.
    """
    name_to_id = {}
    for p in participants:
        n = _norm(p.name).strip()
        if len(n) >= 4:
            name_to_id[n] = p.id

    links: dict[int, set[int]] = {}
    for p in participants:
        text = _norm(p.who_to_meet)
        if not text:
            continue
        for name, qid in name_to_id.items():
            if qid != p.id and name in text:
                links.setdefault(p.id, set()).add(qid)
    return links

# Human-readable labels for each weighted field, shown to the model.
FIELD_LABELS = {
    "groups": "Chosen themes (the predefined groups they checked)",
    "topic": "Specific topic of their work",
    "keywords": "Career-interest keywords",
    "objectives": "Objectives they want to pursue",
    "multidisciplinary": "Multidisciplinary preference",
    "who_to_meet": "People/fields they want to meet",
    "comments": "Free-text matching comments",
    "organization": "Organization",
    "position": "Position",
}


@dataclass
class Group:
    name: str
    member_ids: list[int]
    rationale: str = ""
    flags: list[str] = None  # noqa: RUF013

    def __post_init__(self):
        if self.flags is None:
            self.flags = []


SYSTEM_PROMPT = (
    "You are an expert program coordinator for IMFAHE's Peer Mentoring Circles. "
    "Your job is to sort applicants into small mentoring circles whose members "
    "share themes, goals, and complementary expertise, so they can collaborate "
    "on joint articles, grants, innovation projects, or mutual mentorship. "
    "You weigh each applicant's form answers according to the importance weights "
    "given to you, and you respect each person's stated preference about whether "
    "they want a same-field or multidisciplinary circle. You group people by "
    "genuine affinity, not by forcing one circle per theme. Every applicant must "
    "be placed in exactly one circle, and every circle must have 3 to 6 members. "
    "The program exists to connect people ACROSS institutions, so you avoid "
    "placing two people from the same organization in the same circle, and you try "
    "to honor explicit requests to be grouped with a specific named person."
)


def _participant_block(p: Participant, by_id: dict[int, Participant], wants: set[int]) -> str:
    lines = [f"### Applicant {p.id}: {p.name}"]
    if p.organization:
        lines.append(f"- Organization: {p.organization}")
    if p.position:
        lines.append(f"- Position: {p.position}")
    if p.groups:
        lines.append(f"- Chosen themes: {', '.join(p.groups)}")
    if p.topic:
        lines.append(f"- Specific topic: {p.topic}")
    if p.keywords:
        lines.append(f"- Keywords: {p.keywords}")
    if p.objectives:
        lines.append(f"- Objectives: {', '.join(p.objectives)}")
    if p.multidisciplinary:
        lines.append(f"- Multidisciplinary preference: {p.multidisciplinary}")
    if p.who_to_meet:
        lines.append(f"- Wants to meet: {p.who_to_meet}")
    if wants:
        named = ", ".join(f"{by_id[q].name} (applicant {q})" for q in sorted(wants) if q in by_id)
        lines.append(f"- ** Explicitly named these applicants to be grouped with: {named} **")
    if p.comments:
        lines.append(f"- Comments: {p.comments}")
    return "\n".join(lines)


def build_user_prompt(
    participants: list[Participant],
    weights: dict[str, int],
    ideal: int,
    min_size: int,
    max_size: int,
    themes: list[str],
) -> str:
    n = len(participants)
    approx_groups = max(1, round(n / ideal))
    by_id = {p.id: p for p in participants}
    meet_links = compute_meet_links(participants)

    weight_lines = []
    for field_name, label in FIELD_LABELS.items():
        w = weights.get(field_name, 0)
        if w > 0:
            weight_lines.append(f"- {label}: importance {w}/5")
    weights_text = "\n".join(weight_lines)

    roster = "\n\n".join(
        _participant_block(p, by_id, meet_links.get(p.id, set())) for p in participants
    )

    return f"""# Task
Sort the {n} applicants below into Peer Mentoring Circles based on genuine shared
interest and complementary expertise.

# Group-size rules (strict)
- Each circle MUST have between {min_size} and {max_size} members. Never fewer
  than {min_size}; never more than {max_size}.
- Aim for circles of about {ideal} members (4–5 is ideal). With {n} applicants,
  that is roughly {approx_groups} circles.
- Every applicant must be placed in exactly one circle. Nobody is left out.

# Hard rules
- Do NOT place two applicants from the SAME organization in the same circle. Only
  break this if it is genuinely unavoidable to keep every circle at 3–6 members.
- If an applicant explicitly named another applicant to be grouped with (flagged
  in their block), put them in the same circle whenever possible.

# How circles relate to themes
- The theme list below is an INPUT signal, not the output structure. Do NOT make
  exactly one circle per theme.
- A popular theme should become SEVERAL circles (e.g. many neuroscientists -> two
  or three neuroscience circles split by sub-topic).
- A thinly-chosen theme need NOT have its own circle — fold those people into the
  most related circle.
- Name each circle descriptively; you may combine themes in the name
  (e.g. "Eye Science & Neuroscience", "Omics & Microbiology").

# How to weigh the information (0 = ignore, 5 = drives grouping)
{weights_text}

# Honor the multidisciplinary preference per person
- "prefer experts within my field" -> keep their circle thematically homogeneous.
- "multidisciplinary team" -> it is good to mix complementary fields in their circle.
- "happy with any assignment" -> flexible; use them to balance circles.

# Reference theme list
{", ".join(themes)}

# Applicants
{roster}

# Output format
Return a single JSON object exactly like this:
{{
  "groups": [
    {{
      "name": "Short descriptive circle name, e.g. 'Eye Science & Neuroscience'",
      "member_ids": [0, 4, 9],
      "rationale": "One sentence on what unites this circle.",
      "flags": ["Optional notes on any member who is a borderline fit."]
    }}
  ]
}}
Use the integer applicant IDs shown above in member_ids. Do not invent IDs.
Include every applicant ID exactly once across all groups.
"""


def generate_groups(
    client: LLMClient,
    participants: list[Participant],
    weights: dict[str, int],
    ideal: int,
    min_size: int,
    max_size: int,
    themes: list[str],
) -> list[Group]:
    """Call the model and return the parsed circles it proposes.

    Only valid, de-duplicated applicant IDs are kept. Enforcing 3–6 sizes and
    placing everyone is the balancer's job (see matching.balance), so this stays
    a thin parse of the model output.
    """
    valid_ids = {p.id for p in participants}
    user = build_user_prompt(participants, weights, ideal, min_size, max_size, themes)
    raw = client.complete(SYSTEM_PROMPT, user)
    data = parse_json_response(raw)

    groups: list[Group] = []
    seen: set[int] = set()
    for g in data.get("groups", []):
        ids = []
        for i in g.get("member_ids", []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in valid_ids and i not in seen:
                ids.append(i)
                seen.add(i)
        groups.append(
            Group(
                name=str(g.get("name", "Unnamed circle")).strip() or "Unnamed circle",
                member_ids=ids,
                rationale=str(g.get("rationale", "")).strip(),
                flags=[str(f) for f in g.get("flags", []) if str(f).strip()],
            )
        )
    return groups
