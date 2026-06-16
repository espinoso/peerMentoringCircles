"""Load and validate a 2027 Peer Mentoring participation-form export.

The app targets the 2027 Google Form format only. Google Forms exports use the
full question text as the column header, so we map by tolerant substring match
on a distinctive phrase from each question. That keeps us robust to small edits
(extra spaces, punctuation) without needing a manual mapping screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import pandas as pd

# Internal field name -> list of lowercase phrases; the first column header that
# contains any phrase wins. Order matters (most specific first).
COLUMN_MATCHERS: dict[str, list[str]] = {
    "name": ["full name"],
    "email": ["e-mail", "email"],
    "organization": ["organization where you work"],
    "position": ["position within you", "position within your"],
    "topic": ["specific topic of your work"],
    "keywords": ["keywords that describe"],
    "who_to_meet": ["someone within imfahe", "would like to meet"],
    "objectives": ["which of the following objectives"],
    "groups": ["which of these groups would you like to join"],
    "multidisciplinary": ["experts from other fields", "multidisciplinary"],
    "comments": ["comments to help us", "better matching"],
}

# Fields that must be present for matching to make sense.
REQUIRED_FIELDS = ["name", "email", "groups", "topic"]

# Multi-select fields are stored by Google Forms as ";"-joined strings.
MULTI_FIELDS = ["objectives", "groups"]


@dataclass
class Participant:
    """One applicant, normalized from a form row."""

    id: int
    name: str = ""
    email: str = ""
    organization: str = ""
    position: str = ""
    topic: str = ""
    keywords: str = ""
    who_to_meet: str = ""
    objectives: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    multidisciplinary: str = ""
    comments: str = ""

    def display(self) -> str:
        org = f" ({self.organization})" if self.organization else ""
        return f"{self.name}{org}"


class ParseError(Exception):
    """Raised when the uploaded file can't be used for matching."""


def _all_phrases() -> list[str]:
    return [phrase for phrases in COLUMN_MATCHERS.values() for phrase in phrases]


def _detect_header_row(raw: pd.DataFrame) -> int:
    """Find the row that best looks like the column headers.

    A clean Google Forms export has headers in row 0, but a manually saved sheet
    may have a title row first. We pick the row (within the first few) that
    matches the most known question phrases.
    """
    phrases = _all_phrases()
    best_row, best_score = 0, -1
    for i in range(min(5, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        score = sum(1 for ph in phrases if any(ph in cell for cell in cells))
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def _read_table(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith(".csv"):
        raw = pd.read_csv(BytesIO(data), dtype=str, keep_default_na=False, header=None)
    elif name.endswith((".xlsx", ".xls")):
        raw = pd.read_excel(BytesIO(data), dtype=str, keep_default_na=False, header=None)
    else:
        raise ParseError("Please upload a .csv or .xlsx file exported from the form.")

    header_row = _detect_header_row(raw)
    df = raw.iloc[header_row + 1 :].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row].tolist()]
    df = df.reset_index(drop=True)
    return df


def _build_column_map(columns: list[str]) -> dict[str, str]:
    """Map internal field names to the actual column headers in the file."""
    lowered = {col: str(col).strip().lower() for col in columns}
    mapping: dict[str, str] = {}
    for field_name, phrases in COLUMN_MATCHERS.items():
        for col, low in lowered.items():
            if any(phrase in low for phrase in phrases):
                mapping[field_name] = col
                break
    return mapping


def _split_multi(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(";") if part.strip()]


def parse_participants(data: bytes, filename: str) -> tuple[list[Participant], dict[str, str]]:
    """Parse the upload into Participants. Returns (participants, column_map).

    Raises ParseError with a human-readable message on any fatal problem.
    """
    df = _read_table(data, filename)
    if df.empty:
        raise ParseError("The file has no rows.")

    column_map = _build_column_map(list(df.columns))

    missing = [f for f in REQUIRED_FIELDS if f not in column_map]
    if missing:
        pretty = {
            "name": "Full Name",
            "email": "E-mail address",
            "groups": "Which of these groups would you like to join",
            "topic": "Specific topic of your work",
        }
        names = ", ".join(pretty.get(m, m) for m in missing)
        raise ParseError(
            "Could not find these expected columns in the file: "
            f"{names}. Is this the 2027 participation-form export?"
        )

    participants: list[Participant] = []
    for idx, row in df.iterrows():
        def get(field_name: str) -> str:
            col = column_map.get(field_name)
            return str(row[col]).strip() if col is not None else ""

        # Skip blank rows (no name and no email).
        if not get("name") and not get("email"):
            continue

        participants.append(
            Participant(
                id=len(participants),
                name=get("name"),
                email=get("email"),
                organization=get("organization"),
                position=get("position"),
                topic=get("topic"),
                keywords=get("keywords"),
                who_to_meet=get("who_to_meet"),
                objectives=_split_multi(get("objectives")),
                groups=_split_multi(get("groups")),
                multidisciplinary=get("multidisciplinary"),
                comments=get("comments"),
            )
        )

    if not participants:
        raise ParseError("No participant rows found (all rows were empty).")

    return participants, column_map


@dataclass
class DuplicateCluster:
    """A set of rows that look like the same person."""

    reason: str  # e.g. "same e-mail" / "same name"
    members: list[Participant]
    suggested_keep_id: int  # the row we suggest keeping (latest submission)


def find_duplicates(participants: list[Participant]) -> list[DuplicateCluster]:
    """Group rows that share an e-mail (or, failing that, an identical name).

    Form rows are in submission order, so the last row in a cluster is the most
    recent submission — that's the one we suggest keeping.
    """
    clusters: list[DuplicateCluster] = []
    covered: set[int] = set()

    by_email: dict[str, list[Participant]] = {}
    for p in participants:
        key = p.email.strip().lower()
        if key:
            by_email.setdefault(key, []).append(p)
    for rows in by_email.values():
        if len(rows) > 1:
            clusters.append(
                DuplicateCluster("same e-mail", rows, suggested_keep_id=rows[-1].id)
            )
            covered.update(p.id for p in rows)

    by_name: dict[str, list[Participant]] = {}
    for p in participants:
        if p.id in covered:
            continue
        key = p.name.strip().lower()
        if key:
            by_name.setdefault(key, []).append(p)
    for rows in by_name.values():
        if len(rows) > 1:
            clusters.append(
                DuplicateCluster("same name", rows, suggested_keep_id=rows[-1].id)
            )

    return clusters
