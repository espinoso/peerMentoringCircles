# Peer Mentoring Circles — Matching Tool Revamp (Design)

Date: 2026-10-26
Status: Draft for approval

## Intent & why

The IMFAHE Peer Mentoring matching tool works and is live on Streamlit Cloud,
but it was built a while ago and the owner wants it modernized and brought up
to current standards. Real-world use (Maria's June 2026 review) showed the
output is *similar to the manual process* and a good starting point — so the
job is to **match or beat that quality** while removing the main friction.

The friction is in the **front half** of the workflow: cleaning messy
registration data, and running the matcher several times before the groups
feel right. We reduce both, keep the tool organizers already know, and leave
the repo in a state that is safe to change (tests + CI + security scanning).

## Goals

- **Quality: match or beat today's output.** Specifically fix the two rough
  edges Maria flagged — the "malformed last group" and individual misfits.
  Proven by an automated test suite over the balancer plus one manual review
  run on the sample data. **Misfits** are handled by *surfacing* them reliably
  (clear borderline-fit flags + one-click move) plus *light* prompt tuning —
  not open-ended auto-minimization; the manual review is the backstop.
- **Smarter, review-first data cleanup.** Auto-*flag* (never auto-delete):
  duplicate submissions (email/name) with a clear keep/drop view; blank or
  unusable rows (no themes, no topic); and normalized theme labels (strip the
  `"13. "` numbering the form adds). Plus a fast "exclude anyone" control so
  the organizer can drop people the data can't flag on its own.
- **Fewer, cleaner iterations.** The organizer can *lock* circles/people they
  are happy with; a regenerate reshuffles only the unlocked ones, so edits
  survive and converging no longer needs several full passes. (No full
  edit-merge engine.)
- **Cleaner UI** for non-technical organizers (via Impeccable).
- **Light in-context guidance.** Because the tool runs once a year or less, each
  step carries a short plain-language note on *what to do and why* (where to
  export the form, what the weight sliders change, why same-org pairs get
  split, what "lock" does). Unobtrusive and secondary — brief captions /
  collapsible help, never crowding the screen or adding fluff.
- **Claude-only**, `claude-opus-5-5`. OpenAI support removed entirely.
- **Repo to current standard:** pytest suite, GitHub Actions CI (ruff +
  pytest), the `anthropics/claude-code-security-review` Action on PRs, and
  Dependabot (security alerts + grouped monthly version bumps).

## Non-goals / anti-goals

- **No live Google Forms/Sheets integration.** Keep the upload-a-file model.
- **No distribution features** (emailing groups, scheduling, the Collaborative
  Plan docs) — out of scope, same as the original.
- **Never silently drop or alter a person's data.** All cleanup is
  review-and-confirm. Dropping someone is always the organizer's explicit act.
- **Do not promise to auto-detect "can't participate" rows.** In the 2025 data
  that status was marked *only by red cell fill*, which does not survive a form
  export — it is human knowledge, not data. We make manual exclusion fast
  instead of pretending to detect it.
- **No embeddings/clustering.** The design stays LLM-first.
- **No full edit-merge/tracking engine**, and **no open-ended auto-tuning** to
  chase a subjective misfit target — lock/regenerate + surfaced flags + the
  manual review run are the backstop.
- **No over-engineering** (ponytail): keep the existing module layout, add no
  new abstractions, wrappers, or config nobody asked for.

## Constraints

- Users are non-technical IMFAHE organizers: "open a link, upload, click,
  download." Single magic-word login. Hosted on Streamlit Community Cloud
  (free), which only redeploys on push — it runs no checks, so CI is the only
  safety net before the live app updates.
- Cost is trivial (one LLM call, a few runs per year), so model choice is
  about quality, not price.
- `claude-opus-5-5` rejects a custom `temperature` (the current Anthropic path
  hardcodes `temperature=0.2`, which would 400) — must be removed.
- Targets the 2027 participation-form export. **No real 2027 responses exist
  yet**; the test data is a synthetic 2027-shaped file plus the real 2025 file
  (used under extrapolation). The 2025 column *wording and order* differ from
  2027, so the parser must keep matching columns by tolerant phrase, not
  position.
- PII stays out of git (`.gitignore` already excludes `samples/`,
  `supporting_docs/`, `*.xlsx`, `*.csv`, and `.streamlit/secrets.toml`).

## The how (decided)

Keep the module layout: `app.py`, `matching/{parser,prompt,llm,balance,export}.py`,
`themes.py`, `config.toml`, `scripts/run_match.py`.

- **`llm.py`** — delete the OpenAI backend and the reasoning-model/temperature
  branching; keep one Anthropic path; remove `temperature`; model from config.
- **`config.toml`** — drop the `[provider]` openai/anthropic switch down to a
  single Anthropic model setting; keep weights and group sizes.
- **`parser.py`** — normalize theme labels (strip leading `"<n>. "`) so they
  match `themes.py`; keep tolerant phrase-based column mapping.
- **Cleanup step** — generalize today's duplicates section into one "Review &
  clean" step: duplicates (keep/drop), flagged unusable rows, and an
  exclude-anyone control. Review-and-confirm only.
- **Iteration (lock + regenerate)** — the organizer locks circles/people they
  are happy with; regenerate only reshuffles the unlocked ones. Locked members
  are passed to the balancer as fixed (it already has a `locked` concept for
  named-request must-links — reuse it). Exact UI is the builder's.
- **Balancer last-group fix** — "malformed last group" is interpreted as the
  thematically-incoherent leftover circle from the orphan/merge path (sizes are
  already guaranteed, so it is not a size bug). Reproduce by running the
  pipeline on the 2025 data, then lock it in with a coherence-proxy regression
  test (e.g. the final circle's intra-group affinity is not far below the rest)
  plus the manual review run.
- **Tests (pytest, no live API calls — mock the model)** — parser, balancer
  invariants (everyone placed exactly once; every circle within min/max;
  same-organization pairs separated when avoidable; named "meet" requests and
  locked members honored; no incoherent leftover group); prompt JSON parsing;
  export layout. **Fixtures:** small, hand-written, fully-fake files committed
  to the repo (no PII) — the real 2025/2027 files stay gitignored for
  local/manual runs. One fixture is built to the **real 2027 export headers**,
  taken from the 2027 form PDF (`supporting_docs/Peer Mentoring Circles 2027-
  Participation Form..pdf`) / the live form, so phrase-based column matching is
  verified against genuine 2027 wording, not a 2025 clone.
- **Model** — `claude-opus-5-5` is confirmed current/valid; verify the key's
  entitlement with one tiny live call before merge; pin a current model for the
  security-review Action.
- **CI** — `.github/workflows/ci.yml` (ruff + pytest on push/PR);
  `.github/workflows/security.yml` (claude-code-security-review on PRs, model
  pinned to a current one, key from `CLAUDE_API_KEY` secret); `dependabot.yml`
  (pip ecosystem, monthly grouped updates, security alerts on).
- **UI** — an Impeccable pass over the Streamlit app, including the light
  in-context guidance above (own slice; mockups reviewed before coding).

## Left to the builder

- Exact UI for lock/regenerate (the lock concept itself is decided above).
- Exact balancer change that removes the incoherent leftover circle.
- UI specifics (driven by the Impeccable pass).
- Whether `scripts/run_match.py` stays (likely yes, trimmed to Anthropic-only)
  or is dropped.
- Whether local secrets move to a `.env` + `python-dotenv` convenience (the
  app already reads `.streamlit/secrets.toml`, so this is optional polish).
