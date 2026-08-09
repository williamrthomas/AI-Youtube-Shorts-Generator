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
| Layout, typography, timing and safe margins are code, not per-episode whim | `render/design.py`, `render/scene_plan.py` |

---

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

mpvf init            # data dirs, database, starter templates, pronunciation lexicon
mpvf doctor          # check ffmpeg, playwright, ollama, kokoro, rasterizer, credentials
mpvf templates list
mpvf run coastal-under-1m --dry-run
mpvf serve           # dashboard on http://127.0.0.1:8765
```

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
  generation/    provider interface (Ollama, deterministic, fallback chain)
  scripting/     versioned prompts, schemas, fact validator, anti-slop gate
  speech/        Maine pronunciation lexicon, Kokoro TTS, captions, alignment
  render/        scene plan, SVG design system, FFmpeg graph, thumbnails
  qa/            factual, editorial, visual, audio and technical checks
  publish/       metadata, publication package, resumable YouTube upload
  web/           FastAPI + Jinja dashboard
  cli/           Typer commands, doctor, scaffolding, retention cleanup
config/          settings, templates, pronunciation, verification domains
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

Requires real dependencies to exercise live: Playwright (discovery), Ollama
(model-written prose — the deterministic writer covers the rest), Kokoro
(narration audio), FFmpeg (encoding), an SVG rasterizer (burned-in overlays),
and YouTube OAuth (upload).

See `docs/operator-guide.md` for the pilot procedure and `docs/architecture.md`
for how the pieces fit.
