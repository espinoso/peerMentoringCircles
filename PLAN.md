# IMFAHE Peer Mentoring Circles — Matching App: Plan (LEAN / LLM-first)

> Status: **APPROVED — building.** Architecture is LLM-first: the model does the
> matching; the app is the operator UX + consistent formatted export.

---

## 1. Goal & scope

Replace the manual clustering of Peer Mentoring Circle applicants with an
**assisted, editable** tool. The app sends the cohort to an LLM (with per-question
weighting and group-size constraints), gets back proposed circles of **3–6**, lets
a non-technical organizer review/edit, then exports a grouped XLSX shaped like the
2025 *"Groups Organize by topics"* sheet.

- **Works exclusively with the 2027 form format.** Past data is reference only.
- Out of scope: sending emails, scheduling, the Collaborative Plan docs.

## 2. Why LLM-first (not embeddings + clustering)

The matching intelligence is the model. ~125 participants + their free text fit
easily in context, and the desired behavior is "a model assesses everyone with all
the info." So we drop embeddings/clustering entirely — which also removes the
PyTorch/memory risk on Streamlit Cloud. Less code, fewer moving parts.

## 3. Users & deployment

- Non-technical IMFAHE organizers: "go to a link, upload, click, download."
- Streamlit, hosted on Streamlit Community Cloud (free), single **magic-word**
  password (link is public-but-unlisted). Redeployable elsewhere unchanged.

## 4. Data input

- 2027 Google Form export (CSV/XLSX). Known column schema is targeted directly
  (with tolerant header matching); file is validated on upload and the parsed
  participants are previewed before matching.

## 5. Signals & per-question weights

Every form question is an independent signal with its own **0–5 weight slider**
(defaults in `config.toml`). Weights are rendered into the prompt as relative
importance — the model is told how much each field should drive grouping.

| Field | Form Q | Default weight |
|-------|--------|----------------|
| chosen themes | Q9 | 5 |
| specific topic | Q5 | 4 |
| keywords | Q6 | 4 |
| objectives | Q8 | 3 |
| multidisciplinary pref | Q10 | 3 |
| who to meet | Q7 | 2 |
| comments | Q11 | 2 |
| organization | Q3 | 1 |
| position | Q4 | 1 |

Q10 is handled **independently** (not folded into theme logic): "prefer same field"
biases toward homogeneous circles, "multidisciplinary" toward mixed, per person.

## 6. Group sizing (evidence-based)

Research on collaborative/peer-learning groups points to **3–4 as the sweet spot**
(even participation, low free-riding); larger adds diversity but more loafing.
Syllabus allows 3–6 and attendance attrition is expected, so default **ideal 5,
min 3, max 6**. Per theme/cluster the model aims `n_groups ≈ round(demand/5)` and
balances toward the ideal rather than leaving a starved group. All configurable.

## 7. Matching call

`matching/prompt.py` builds one structured prompt: participant roster (all mapped
fields), the weights, size constraints, the editable theme list, and instructions
to (a) form circles, (b) name each, (c) give a one-line rationale, (d) flag weak
fits, (e) honor each person's Q10 preference. The model returns **JSON** parsed
into groups. Cross-circle reasoning is allowed (global view).

## 8. Provider abstraction (swappable)

`matching/llm.py` — one interface, two backends behind a config switch:
`openai` (default; key already in hand) and `anthropic`. Models set in
`config.toml`; key from Streamlit secret / env var. No other code changes to swap.

## 9. Edit & export

- Edit UI: every participant has a "which circle" selector; rename circles;
  add/remove circles; live size warnings (<3 / >6); regenerate button.
- Export: XLSX laid out like *"Groups Organize by topics"* — circle name as a
  section header, members beneath with full columns, blank row between circles.

## 10. Project structure

```
peerMentoring/
  app.py                 # Streamlit flow: upload → preview → weights → generate → edit → export
  matching/
    __init__.py
    parser.py            # load + validate 2027 CSV/XLSX into Participant rows
    prompt.py            # build the weighted matching prompt + parse JSON result
    llm.py               # provider-agnostic client (openai | anthropic)
    export.py            # write grouped XLSX
  themes.py              # editable 21-theme list + objective/multidisciplinary options
  config.toml            # provider, models, weights, group sizes
  requirements.txt
  README.md              # plain-language run & deploy guide
  .streamlit/
    secrets.toml.example # template for API key + app password
  PLAN.md
  supporting_docs/       # existing reference material (incl. 2025 data for testing)
```

## 11. Build sequence

1. Schema + parser + file validation/preview (test against 2025 data).
2. Weight UI + prompt builder + provider layer + JSON parsing.
3. Editable groups UI + size warnings.
4. XLSX export matching last year's layout.
5. Password gate + README + deploy notes.

## 12. Settled decisions

- Auth: single magic-word password. ✅
- Each form question = independent weighted signal; Q10 not folded in. ✅
- Per-question weight sliders; **2027 format only**. ✅
- Group size: ideal 5, min 3, max 6. ✅
- LLM may suggest cross-circle moves. ✅
- No embeddings (LLM-first). ✅
- Default provider OpenAI, swappable to Anthropic. ✅
