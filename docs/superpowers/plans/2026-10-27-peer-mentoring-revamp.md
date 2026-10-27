# Peer Mentoring Circles Revamp — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modernize the IMFAHE Peer Mentoring matching tool — Claude-only on Opus 5.5, smarter review-first cleanup, lock-and-regenerate, a fixed last-group, light in-context guidance, and a tested repo with CI — while keeping the upload→review→generate→edit→download shape.

**Architecture:** Keep the existing module layout (`app.py` + `matching/{parser,prompt,llm,balance,export}.py` + `themes.py` + `config.toml`). Logic lives in `matching/`, pure and testable; `app.py` stays a thin Streamlit orchestration layer. A new `tests/` suite with fake, committed fixtures is the safety net Streamlit Cloud's auto-deploy lacks.

**Tech Stack:** Python 3.11, Streamlit, pandas, openpyxl, `anthropic` SDK, pytest, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-26-peer-mentoring-revamp-design.md`

## Global Constraints

- **Claude-only.** Model `claude-opus-5-5`. No OpenAI code anywhere.
- **No custom `temperature`** on the Anthropic call (Opus 5.5 returns 400).
- **Python 3.11+** (stdlib `tomllib`).
- **Never auto-delete or auto-alter a person's data** — all cleanup is review-and-confirm; exclusion is an explicit organizer action.
- **No PII in git.** Test fixtures are fully fake. `.gitignore` already excludes `samples/`, `supporting_docs/`, `*.xlsx`, `*.csv`, `.streamlit/secrets.toml`; test fixtures live under `tests/fixtures/` and must be named so they are NOT caught by `*.csv`/`*.xlsx` ignores — commit them explicitly with `git add -f` OR add a `!tests/fixtures/` negation to `.gitignore` (do the negation; it is clearer).
- **No over-engineering (ponytail):** least code that fully solves the task; no new abstractions, wrappers, or config nobody asked for; reuse what exists.
- Secret name for the security Action: `CLAUDE_API_KEY`.

## Review Focus

- **A real 2027 export with slightly different header wording** than our fixture — tolerant phrase matching must still map every required column (Task 2 test covers both 2025 and 2027 header styles; real 2027 export does not yet exist, so this stays a known residual risk).
- **Wrong file uploaded** (not the participation form) — must raise a clear `ParseError`, never a stack trace (Task 2).
- **Tiny cohort** (< `min_size` total applicants) — balancer must terminate and emit a human note, not loop or crash (Task 4).
- **Lock then regenerate leaving too few unlocked people** to form a valid circle — must not crash or silently drop people; the engine's tiny-cohort path (Task 4 test) plus the app-level skip-with-note (Task 7) cover it.
- **Bad/unentitled API key or model 400** — must show a friendly error in the UI, not a traceback (Task 1).

---

### Task 1: Claude-only LLM client

**Files:**
- Modify: `matching/llm.py` (remove OpenAI backend + provider branching + temperature)
- Modify: `config.toml` (replace `[provider]` block with a single model)
- Modify: `app.py` (`_run_generation`, `sidebar_controls` — drop provider selectbox)
- Modify: `scripts/run_match.py` (drop provider/key branching)
- Create: `tests/test_llm.py`, `pyproject.toml` (pytest + ruff config), `requirements-dev.txt`
- Modify: `.gitignore` (add `!tests/fixtures/`)

**Interfaces:**
- Produces: `LLMClient(model: str, api_key: str)` with `.complete(system: str, user: str) -> str`; raises `LLMError` on empty key or request failure. `parse_json_response(text: str) -> dict` unchanged.

- [ ] **Step 1: Write failing tests** in `tests/test_llm.py`: `test_complete_calls_anthropic_without_temperature` (monkeypatch `anthropic.Anthropic` with a fake whose `messages.create` records kwargs; assert `"temperature" not in kwargs` and the returned text is the fake's text); `test_empty_key_raises_llmerror`.
- [ ] **Step 2: Run** `pytest tests/test_llm.py -v` — expect FAIL (import error / temperature still sent).
- [ ] **Step 3:** Rewrite `LLMClient` to a single Anthropic path: constructor `(model, api_key)`; `complete()` calls `anthropic.Anthropic(api_key=...).messages.create(model=self.model, max_tokens=16000, system=system + "\n\nRespond with a single valid JSON object and nothing else.", messages=[{"role":"user","content":user}])` and joins text blocks; no `temperature`, no `_openai`/`_anthropic`/`_is_reasoning_model`. Wrap errors in `LLMError`. Add `pyproject.toml` with `[tool.pytest.ini_options]` (`testpaths=["tests"]`) and a basic `[tool.ruff]`. `requirements-dev.txt`: `pytest`, `ruff`.
- [ ] **Step 4:** Update `config.toml` to `[provider]\nmodel = "claude-opus-5-5"` (drop openai/anthropic sub-tables). Update `app.py` `_run_generation` to `LLMClient(settings["model"], api_key)` reading `ANTHROPIC_API_KEY`; remove the provider selectbox from `sidebar_controls` and the caption; `settings["model"]` comes from `cfg["provider"]["model"]`. Update `scripts/run_match.py` the same way. Confirm the existing `except LLMError` in `_run_generation` still renders `st.error` (Review Focus: bad key).
- [ ] **Step 5: Run** `pytest tests/test_llm.py -v` — expect PASS.
- [ ] **Step 6: Commit** `refactor: Claude-only LLM client on opus-5-5, drop OpenAI`.

### Task 2: Parser — theme normalization + fixtures + tests

**Files:**
- Modify: `matching/parser.py` (`_split_multi` / new `_normalize_theme`)
- Create: `tests/fixtures/responses_2025.csv`, `tests/fixtures/responses_2027.csv` (fake data), `tests/test_parser.py`

**Interfaces:**
- Produces: `parse_participants(data: bytes, filename: str) -> tuple[list[Participant], dict]` unchanged; `Participant.groups` values have no leading `"<n>. "` numbering.

- [ ] **Step 1:** Build fixtures. `responses_2025.csv` uses the real 2025 header row verbatim (title row above it to exercise header detection): `Timestamp, Full Name, E-mail address, Organization where you work in, Position within you organization, Specific topic of your work, Keywords that describe your career interests, Is there someone within IMFAHE´s Network that you would like to meet, Which of the following objectives would you like to work on, Which of these groups would you like to join?, Would you like to have in your group experts from other fields, Include any comments to help us to make a better matching, By sending this form you agree`. `responses_2027.csv` uses the **real 2027 question wording extracted from** `supporting_docs/Peer Mentoring Circles 2027- Participation Form..pdf` (read the PDF; if a question's export header is ambiguous, keep the 2025 phrasing and note it). Both files: ~8 fully fake applicants, fake names/emails, including one duplicate email, one row with numbered themes like `13. Technology and Robotics; 19. Psychology`, one row missing themes, two from the same organization.
- [ ] **Step 2: Write failing tests** in `tests/test_parser.py`: `test_parses_2025_headers` (124-style mapping: all of name/email/groups/topic present); `test_parses_2027_headers`; `test_detects_header_row_under_title`; `test_strips_theme_numbering` (asserts `"Technology and Robotics"` in groups, not `"13. Technology and Robotics"`); `test_wrong_file_raises_parseerror` (feed a CSV with none of the known columns → `ParseError`); `test_duplicate_email_detected` (via `find_duplicates`).
- [ ] **Step 3: Run** `pytest tests/test_parser.py -v` — expect FAIL on `test_strips_theme_numbering`.
- [ ] **Step 4:** Add `_normalize_theme(label: str) -> str` that strips a leading `^\s*\d+\.\s*` and apply it to each value when the field is `groups` inside `_split_multi` (or in the `groups=` construction). Leave other multi fields untouched.
- [ ] **Step 5: Run** `pytest tests/test_parser.py -v` — expect PASS.
- [ ] **Step 6: Commit** `feat: normalize numbered theme labels; add parser tests + fake fixtures`.

### Task 3: Smarter cleanup — flag unusable rows

**Files:**
- Modify: `matching/parser.py` (add `find_unusable`)
- Modify: `tests/test_parser.py`

**Interfaces:**
- Produces: `find_unusable(participants: list[Participant]) -> list[tuple[Participant, str]]` — each flagged participant with a plain-language reason (`"no themes chosen"`, `"no topic given"`). Pure; never mutates.

- [ ] **Step 1: Write failing test** `test_find_unusable_flags_missing_themes_and_topic` using the themeless fixture row; assert the row appears with reason mentioning themes.
- [ ] **Step 2: Run** — expect FAIL (no `find_unusable`).
- [ ] **Step 3:** Implement `find_unusable`: flag participants with empty `groups` or empty `topic`; return `(participant, reason)` pairs. No deletion.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `feat: flag unusable rows for review (no themes / no topic)`.

### Task 4: Balancer — invariants + tiny-cohort safety + last-group coherence

**Files:**
- Modify: `matching/balance.py` (orphan/merge path)
- Create: `tests/test_balance.py`

**Interfaces:**
- Consumes: `Group`, `Participant`, `balance_groups(groups, participants, included_ids, min_size=3, max_size=6, target=6) -> (list[Group], list[str])`.
- Produces: same signature (no new params — locking is handled in `app.py` by balancing only the unlocked pool, Task 7).

- [ ] **Step 1: Write failing tests** in `tests/test_balance.py` using hand-built `Participant`/`Group` objects: `test_everyone_placed_exactly_once`; `test_all_circles_within_min_max`; `test_same_org_separated_when_avoidable`; `test_named_meet_request_honored`; `test_tiny_cohort_terminates` (2 applicants, min 3 → returns with a note, no exception); `test_leftover_member_joins_related_circle` — construct circles where a naive leftover-placement would drop an unrelated applicant into the last circle; assert the leftover lands in the circle sharing its theme (coherence proxy: its affinity to the assigned circle ≥ its affinity to the circle it would have been dumped in).
- [ ] **Step 2: Run** `pytest tests/test_balance.py -v` — expect `test_leftover_member_joins_related_circle` (and possibly tiny-cohort) to FAIL.
- [ ] **Step 3:** Minimal change to the orphan-placement and undersized-merge paths so a leftover prefers the highest-affinity circle with room and only falls back to the emptiest when no themed match exists (exact tweak is the implementer's; the test pins the behavior). Ensure tiny-cohort path emits the existing "outside 3–6" note and returns.
- [ ] **Step 4: Run** — expect PASS (all balancer tests).
- [ ] **Step 5: Commit** `fix: coherent leftover placement; lock in balancer invariants`.

### Task 5: Prompt build/parse tests + light misfit tuning

**Files:**
- Modify: `matching/prompt.py` (flag instruction wording only)
- Create: `tests/test_prompt.py`

- [ ] **Step 1: Write failing tests**: `test_parse_json_strips_fences` (```json fenced object parses); `test_parse_json_raises_on_garbage`; `test_user_prompt_includes_weights_and_roster` (asserts a weighted field label and an applicant name appear); `test_meet_link_flagged_in_block` (a named applicant produces the "Explicitly named" line).
- [ ] **Step 2: Run** — expect FAIL where parsing/wording differs.
- [ ] **Step 3:** Fix only what the tests require; tighten the flag instruction so borderline-fit notes name the person and stay about fit (light tuning — no scorer rework).
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `test: prompt build/parse coverage; tighten misfit flag wording`.

### Task 6: Export layout test

**Files:**
- Create: `tests/test_export.py`

- [ ] **Step 1: Write failing test** `test_xlsx_has_circle_sections`: `build_xlsx(groups, participants)` → load bytes with openpyxl → assert the theme/circle name sits on the first member's row, members follow, and a blank row separates circles (the "Groups Organize by topics" shape).
- [ ] **Step 2: Run** — expect FAIL or PASS; if PASS, it is a characterization test (keep it).
- [ ] **Step 3:** Adjust `build_xlsx` only if the test reveals a layout gap.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `test: pin grouped XLSX layout`.

### Task 7: App wiring — cleanup/exclude, lock+regenerate, guidance

**Files:**
- Modify: `app.py` (`duplicates_section` → general `review_section`; `generate_section`/`_run_generation`; `edit_section`)

**Interfaces:**
- Consumes: `find_unusable` (Task 3), `find_duplicates`, `balance_groups`, `generate_groups`.

- [ ] **Step 1:** Extend the review step: alongside duplicates, list `find_unusable` rows with their reason and the same keep/exclude checkboxes; keep "exclude anyone" available for every applicant (not only flagged ones). Exclusions feed the existing `excluded_ids`. No deletion.
- [ ] **Step 2:** Add a per-circle **lock** toggle in `edit_section` (store `locked_circles: set[str]` by circle name in session state). In `_run_generation`, when locks exist: hold locked groups aside, remove their members from the generation pool, run `generate_groups` + `balance_groups` on the unlocked pool only, then concatenate locked groups back. If locks leave fewer than `min_size` unlocked applicants, skip generation and show a note (Review Focus).
- [ ] **Step 3:** Add light in-context guidance: one short caption per step (upload / review / weights / generate / lock / download) saying what to do and why, plus a one-line "why" on the weight sliders and the same-org rule. Use `st.caption`/`st.expander` — secondary, collapsible, no screen crowding.
- [ ] **Step 4: Verify** by running the app on a fake fixture: upload → see flagged rows → exclude one → generate → lock a circle → regenerate (locked circle unchanged) → download. This is the **first real Anthropic call** — it confirms the `claude-opus-5-5` id and the key's entitlement (design's pre-merge live check). Use the browser preview; capture before/after.
- [ ] **Step 5: Commit** `feat: review-first cleanup, lock+regenerate, in-context guidance`.

### Task 8: UI polish with Impeccable

**Files:**
- Modify: `app.py`, `.streamlit/config.toml`, `themes.py` (presentation only)

- [ ] **Step 1:** Run `/impeccable init` to seed `PRODUCT.md`/`DESIGN.md` for this tool (users = non-technical IMFAHE organizers; goal = calm, guided, once-a-year flow).
- [ ] **Step 2:** Produce mockups of the main screen and the review/edit steps; **review with the user before applying** (visual companion).
- [ ] **Step 3:** Apply the agreed Impeccable pass; run the detector/hooks; keep the guidance from Task 7 unobtrusive.
- [ ] **Step 4: Verify** in the browser preview at desktop and narrow widths; no horizontal scroll, guidance stays secondary.
- [ ] **Step 5: Commit** `style: Impeccable UI pass`.

### Task 9: CI — lint, tests, Dependabot

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/dependabot.yml`

- [ ] **Step 1:** `ci.yml`: on push + pull_request; Python 3.11; `pip install -r requirements.txt -r requirements-dev.txt`; run `ruff check .` and `pytest`.
- [ ] **Step 2:** `dependabot.yml`: ecosystem `pip`, schedule monthly, grouped updates; security updates enabled.
- [ ] **Step 3: Verify:** locally `ruff check .` and `pytest` pass; push a branch and open a PR; confirm the `ci` check runs and goes green.
- [ ] **Step 4: Commit** `ci: ruff + pytest on push/PR; dependabot monthly`.

### Task 10: Security-review Action

**Files:**
- Create: `.github/workflows/security.yml`

- [ ] **Step 1:** Add the `anthropics/claude-code-security-review` Action per its README: `on: pull_request`; permissions `pull-requests: write`, `contents: read`; checkout head with `fetch-depth: 2`; `claude-api-key: ${{ secrets.CLAUDE_API_KEY }}`; `comment-pr: true`; pin `claude-model` to a current model.
- [ ] **Step 2:** Add the `CLAUDE_API_KEY` repo secret (owner action via `gh secret set` or GitHub UI; may reuse the revamp key).
- [ ] **Step 3: Verify:** open a PR and confirm the security job runs (findings post as review comments, or a clean pass).
- [ ] **Step 4: Commit** `ci: add claude-code-security-review on PRs`.
