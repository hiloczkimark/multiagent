# Content pipeline

Give it a topic and it researches the topic, writes, edits, generates a cover image and publishes to a CMS, for under $0.75 and in under 3 minutes.

Status: **step 5 of 6**, the full pipeline: research (quotes checked against the live source pages) → write → edit loop (Opus fact-checks against the brief's quotes, Sonnet revises, Opus re-checks; `eval_report.json`) → verify (Haiku spot-checks 3 claims against quotes and live pages; `claims.json`) → format (SEO, renumbered citations, sources list, HTML preview) → publish **as a draft** to the mock CMS in `cms_mock/`. The cover image is generated in the background during editing: DALL-E when `OPENAI_API_KEY` is set, a placeholder otherwise or if generation fails. Drafts carry review notes when a human should look first (open editor issues, failed spot-checks, placeholder cover).

Latest full run (lithium-ion battery recycling): 141s, $0.41.

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
python cli.py spotcheck <run_id>         # the 3 verified claims with verdict and evidence
python cli.py spotcheck <run_id> --fresh # verify 3 different claims (~$0.01)
python cli.py cms                        # draft posts in the mock CMS (open preview.html in a browser)
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
  schemas.py              contracts passed between stages (ResearchBrief, Draft, EditReview, Revision)
  orchestrator.py         runs the stages in order, supports resuming
  tracking.py             costs.jsonl (one line per API call) and timings.json
  runstore.py             runs/<run_id>/ files
  llm.py                  Anthropic client wrapper that records every call
  livepages.py            fetches source pages ourselves to check quotes
  agents/research.py      stage 1
  agents/write.py         stage 2
  agents/edit.py          stage 3: review -> revise -> re-review, round 2 behind cost/time guards
  agents/image.py         cover image (background stage): Haiku writes the prompt, an ImageProvider draws it
  agents/verify.py        spot-check of 3 claims
  agents/format.py        SEO (Haiku), citations, sources list, Markdown/HTML, review notes
  agents/publish.py       draft to the CMS
  images.py               ImageProvider: OpenAI (plain HTTPS) and placeholder
  cms.py                  CMSAdapter + MockCMS (drafts only; one post per run, updated on re-publish)
  render.py               citation renumbering, Markdown -> HTML, preview page
  prompts/researcher.md   (+ researcher_tools_{dynamic,basic}.md; researcher_v1.md = baseline preset)
  prompts/writer.md  editor.md  reviser.md  image_prompt.md  verifier.md  seo.md
runs/<run_id>/
  run.json  costs.jsonl  timings.json
  01_research.json  01_research_checks.json  01_research_meta.json  01_research_transcript.json
  02_draft.json  02_draft.md  02_draft_checks.json
  03_review_<n>.json  03_draft_r<n>.json/.md  03_edited.json  03_edited.md  eval_report.json
  04_cover.png|svg  04_cover.json  claims.json
  05_article.json  05_article.md  05_article.html  06_published.json
cms_mock/
  index.json  posts/post-<run_id>/{post.json, preview.html, cover}
```

Run time counts foreground stages only; the background image stage adds time only if the
run has to wait for it (`wait_image` in timings.json).

`tests/test_edit.py::test_seeded_fault_is_caught_and_corrected` is the offline check that the
edit loop catches a planted error and corrects it, with the evidence in `eval_report.json`.
