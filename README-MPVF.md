# Maine Property Video Factory (MPVF)

A self-hosted pipeline that discovers Maine real-estate listings, verifies each
one against a second authoritative source, researches local context, writes an
evidence-backed script, narrates it, renders a 1080p episode, runs automated
QA, and publishes a complete package to YouTube.

It is not a text-to-video toy. It is a deterministic production pipeline with
AI-assisted selection, research and writing, where **every spoken fact is
traceable to a stored source**, and where *publishing nothing* is a valid,
first-class outcome.

---

## The rules the code enforces

| Principle | Where it lives |
|---|---|
| Evidence before prose — the writer may only use claims in the bundle | `evidence/bundle.py`, `scripting/fact_validator.py` |
| Prices and statuses come from fields, never model prose | `scripting/generator.py::_inject_structured_facts` |
| Town history is never narrated as house history | `scripting/fact_validator.py::_check_scope_violations` |
| A material conflict blocks a candidate; it is never averaged away | `selection/verification.py` |
| Access challenges are reported, never bypassed | `adapters/zillow/adapter.py`, `adapters/zillow/parser.py` |
| A weak lineup or a weak script skips the episode | `pipeline/stages.py::SkipEpisode` |
| An exhausted pool tries the fallback template — never a relaxed primary | `pipeline/runner.py::run_episode` |
| Layout, typography, timing and safe margins are code, not per-episode whim | `render/design.py`, `render/scene_plan.py` |
| Credentials never reach a prompt, a config file or the database | `generation/cloud.py`, `observability/logging.py` |

---

## Inference and hosting

All inference is hosted — **Cloudflare Workers AI first, OpenRouter second,
and a deterministic writer as the floor** so an outage degrades the episode
rather than failing the run. Narration and transcription go to Workers AI too.

```yaml
provider:
  kind: cloudflare
  model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
  secondary_kind: openrouter
  secondary_model: anthropic/claude-3.5-haiku
  fallback: deterministic
```

Hosting is Cloudflare: a Worker on a cron trigger enqueues the day's episode,
a Container runs the pipeline, artifacts land in R2 and the control plane in
D1. The render tier is a Container rather than a Worker because one episode
shells out to FFmpeg and Chromium — see `docs/cloudflare.md` for that boundary
and for what has and has not been verified live.

---

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

mpvf init            # data dirs, migrated database, starter templates, lexicon
mpvf doctor          # check ffmpeg, playwright, ollama, kokoro, rasterizer, credentials
mpvf templates list
mpvf run coastal-under-1m --dry-run
mpvf serve           # dashboard on http://127.0.0.1:8765
```

The schema is under Alembic; `mpvf db current` shows the applied revision and
`mpvf db check` fails if the models and the database have drifted apart.

`mpvf doctor` is the gate: it tells you exactly which optional component is
missing and what degrades without it. Nothing in the pipeline crashes because
an optional dependency is absent — it reports and, where safe, degrades.

### Running with no external services at all

Every stage works offline against fixtures, which is how CI exercises the whole
pipeline:

```bash
mpvf run coastal-under-1m --fixture-dir tests/fixtures/listings --dry-run
```

---

## Pipeline

```
discover → normalize → verify → select → assets → research → evidence
        → script → narrate → render → qa → publish
```

When today's pool cannot fill a lineup, the run does not simply stop: it starts
a fresh run under the template's configured `fallback_template`, with that
template's own criteria. The primary's rules are never loosened to force an
episode out, and a *quality* failure (bad script, failed QA) never triggers a
fallback — a different template would not fix bad writing.

Each stage reads from the run's artifact directory, writes exactly one
artifact, and can be re-run alone:

```bash
mpvf run coastal-under-1m --run-id run_abc123 --from-stage script --to-stage qa
mpvf run-control resume run_abc123 --from-stage render
```

Run state moves through the §6.1 state machine; illegal jumps raise rather than
silently skipping work. Failure states carry a code, a message and the path to
the saved debug artifact.

---

## Layout

```
src/mpvf/
  acquisition/   browser session, tiny dependency-free HTML/selector engine
  adapters/      zillow (selectors isolated + versioned), brokerage, fixture
  normalization/ address canonicalization, field parsing, deduplication
  selection/     hard filters, rule-based theme fit, scoring, verification
  media/         download, hashing, classification, per-property image choice
  research/      query planning, source capture (MediaWiki API), claims
  evidence/      the bundle: the writer's entire world
  generation/    provider chain: Workers AI, OpenRouter, Ollama, deterministic
  storage/       R2 artifact mirroring, D1 control-plane client
  scripting/     versioned prompts, schemas, fact validator, anti-slop gate
  speech/        Maine pronunciation lexicon, Kokoro TTS, captions, alignment
  render/        scene plan, SVG design system, FFmpeg graph, thumbnails
  qa/            factual, editorial, visual, audio and technical checks
  publish/       metadata, publication package, resumable YouTube upload
  web/           FastAPI + Jinja dashboard
  cli/           Typer commands, doctor, scaffolding, retention cleanup
config/          settings, templates, pronunciation, verification domains
migrations/      Alembic environment and versioned schema revisions
deploy/          wrangler.toml, control-plane Worker, container image
tests/           unit + adapter fixture + offline end-to-end
docs/            architecture, operator guide, source adapters, video design
```

---

## What is deliberately configurable

Model, voice, publish time, price ceilings, geography, music library, title
formula, listing-source priority, exclusion window, whether condos or seasonal
residences are eligible, palette and fonts — all live in `config/`, none in
code.

---

## Status

Implemented and tested offline: the full stage graph, all fixture-driven
parsing, selection, evidence, scripting with fact validation and the editorial
gate, narration and captions, scene planning, the FFmpeg command builder, QA,
the publication package, the dashboard and the CLI.

Hosted inference, R2 mirroring and the D1 client are covered by tests against
mock transports that assert the exact request shapes. What still needs real
credentials or binaries: Playwright (discovery), FFmpeg (encoding), an SVG
rasterizer (overlays), YouTube OAuth (upload), and a first live call against
Workers AI and OpenRouter to confirm their current contracts.

See `docs/operator-guide.md` for the pilot procedure and `docs/architecture.md`
for how the pieces fit.
