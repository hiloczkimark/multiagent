# Content pipeline

Give it a topic and it researches the topic, writes, edits, generates a cover image and publishes to a CMS, for under $0.75 and in under 3 minutes.

Status: **step 2 of 6**: scaffolding, cost and time tracking, the researcher (quotes checked against the live source pages) and the writer.

## Setup (Windows / PowerShell)

```powershell
winget install Python.Python.3.12        # if Python isn't installed yet
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env                   # then put your ANTHROPIC_API_KEY in it
```

## Use

```powershell
python cli.py run "How heat pumps work in cold climates"
python cli.py resume <run_id>            # run the stages whose output is missing
python cli.py resume <run_id> --from write
python cli.py costs <run_id>             # breakdown by agent, with pass/fail on cost and time
python cli.py bench                      # research presets x 5 topics vs stage targets (~$2.50)
python cli.py bench --report runs/bench-<timestamp>
pytest                                   # offline tests; no API calls
```

## Layout

```
cli.py                    run / resume / costs / bench
pipeline/
  config.py               models, prices, research presets, writer settings, budgets and targets
  bench.py                research benchmark (cli.py bench)
  schemas.py              contracts passed between stages (ResearchBrief, Draft)
  orchestrator.py         runs the stages in order, supports resuming
  tracking.py             costs.jsonl (one line per API call) and timings.json
  runstore.py             runs/<run_id>/ files
  llm.py                  Anthropic client wrapper that records every call
  livepages.py            fetches source pages ourselves to check quotes
  agents/research.py      stage 1
  agents/write.py         stage 2
  prompts/researcher.md   (+ researcher_tools_{dynamic,basic}.md; researcher_v1.md = baseline preset)
  prompts/writer.md
runs/<run_id>/
  run.json  costs.jsonl  timings.json
  01_research.json  01_research_checks.json  01_research_meta.json  01_research_transcript.json
  02_draft.json  02_draft.md  02_draft_checks.json
```
