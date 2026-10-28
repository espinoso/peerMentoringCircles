# IMFAHE Peer Mentoring Circles — Matching App

A small Streamlit app that sorts Peer Mentoring Circle applicants into circles of
3–6 people who share themes, goals, and complementary expertise. An LLM does the
matching (weighing every form question by an importance you set); you then review,
move people around, rename circles, and download a grouped spreadsheet.

Works with the **2027 participation-form** export only. Older editions are kept in
`supporting_docs/` purely as reference.

---

## For organizers — how to use it

1. Open the app link and type the access word.
2. In **Google Forms → Responses → link/export to Sheets**, download the responses
   as CSV or XLSX.
3. **Upload** that file. Check the applicant preview looks right.
4. (Optional) In the left sidebar, adjust the **question weights** and **group
   sizes**. Defaults are sensible — themes matter most, ideal circle size is 5.
5. Click **Generate circles**.
6. **Review & edit:** move anyone to a different circle, rename circles, add empty
   ones. Circles outside the 3–6 range are flagged. Click **Apply moves** to commit.
7. Click **Download grouped XLSX**.

No data is stored — everything lives in your browser session and disappears when
you close the tab.

---

## For whoever sets it up

### Run locally

```bash
pip install -r requirements.txt
# create .streamlit/secrets.toml from the example and add your API key
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

If `APP_PASSWORD` is left blank, the login is skipped (handy for local testing).

### Deploy on Streamlit Community Cloud (free)

1. Push this folder to a GitHub repo.
2. On https://share.streamlit.io, create an app pointing at `app.py`.
3. In the app's **Settings → Secrets**, paste the contents of
   `.streamlit/secrets.toml.example` with your real `APP_PASSWORD` and API key.
4. Share the URL. The access word keeps casual visitors out.

### Secrets

Two secrets go in **Settings → Secrets** (or `.streamlit/secrets.toml` locally):

- `ANTHROPIC_API_KEY` — your Claude API key. This is what powers the matching.
- `APP_PASSWORD` — the access word organizers type to open the app. Leave it
  blank to skip the login (handy for local testing).

### Configuration (`config.toml`)

- **Model:** `[provider] model` — the Claude model used for matching
  (`claude-opus-5-5`). There is no provider switch; matching always runs on Claude.
- **Group sizes:** `ideal` / `min` / `max`.
- **Weights:** starting importance (0–5) for each form question; also adjustable
  live in the sidebar.

### Cost

The match is one Claude call per run (a few times a year). For ~125 applicants
that is a few cents; the heavier the model, the better the reasoning.

---

## Project layout

```
app.py             Streamlit UI and flow
matching/parser.py Load & validate the form export
matching/prompt.py Build the weighted prompt, parse the model's JSON
matching/llm.py    Claude API client
matching/export.py Write the grouped XLSX
themes.py          The 21 default themes (editable in-app too)
config.toml        Model, weights, group sizes
PLAN.md            Design decisions and rationale
```
