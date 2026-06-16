"""Headless matching run — for testing the engine without the Streamlit UI.

Usage:
    python scripts/run_match.py [path/to/responses.xlsx]

Reads the API key from the environment (OPENAI_API_KEY / ANTHROPIC_API_KEY) or
from .streamlit/secrets.toml. Prints the proposed circles and writes the grouped
XLSX next to the input file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from matching.balance import balance_groups  # noqa: E402
from matching.export import build_xlsx  # noqa: E402
from matching.llm import LLMClient  # noqa: E402
from matching.parser import find_duplicates, parse_participants  # noqa: E402
from matching.prompt import generate_groups  # noqa: E402
from themes import THEMES  # noqa: E402


def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def get_key(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    secrets = ROOT / ".streamlit" / "secrets.toml"
    if secrets.exists():
        with open(secrets, "rb") as f:
            return tomllib.load(f).get(name, "")
    return ""


def main() -> None:
    cfg = load_config()
    infile = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "sample_2027_responses.xlsx"
    participants, _ = parse_participants(infile.read_bytes(), infile.name)
    print(f"Loaded {len(participants)} applicants from {infile.name}")
    dups = find_duplicates(participants)
    if dups:
        print(f"({len(dups)} possible duplicate cluster(s) detected — kept all in headless mode)")
    print()

    provider = cfg["provider"]["name"]
    model = cfg["provider"][provider]["model"]
    key = get_key("OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY")
    client = LLMClient(provider, model, key)

    m = cfg["matching"]
    print(f"Matching with {provider}:{model} …")
    raw = generate_groups(
        client, participants, dict(cfg["weights"]),
        m["ideal_group_size"], m["min_group_size"], m["max_group_size"], THEMES,
    )
    groups, notes = balance_groups(
        raw, participants, [p.id for p in participants],
        m["min_group_size"], m["max_group_size"], m["ideal_group_size"],
    )

    by_id = {p.id: p for p in participants}
    sizes = [len(g.member_ids) for g in groups if g.member_ids]
    print(f"\n=== {len(sizes)} circles | sizes {sorted(sizes)} | total placed {sum(sizes)} ===\n")
    for g in groups:
        if not g.member_ids:
            continue
        print(f"● {g.name}  ({len(g.member_ids)})")
        if g.rationale:
            print(f"   {g.rationale}")
        for pid in g.member_ids:
            p = by_id[pid]
            print(f"   - {p.name} ({p.organization})")
        for fl in g.flags:
            print(f"   ⚠ {fl}")
        print()
    for nt in notes:
        print("NOTE:", nt)

    out = infile.with_name(infile.stem + "_GROUPED.xlsx")
    out.write_bytes(build_xlsx(groups, participants))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
