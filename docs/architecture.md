# Architecture

## Shape of the system

Three local processes and a scheduler, no queue product, no cluster:

- `mpvf-web` — dashboard and local API (`mpvf serve`)
- `mpvf-worker` — executes one pipeline job at a time (`mpvf run <template>`)
- `systemd` timers / `launchd` — triggers the daily job and health checks

SQLite in WAL mode provides job state, stage status and retry state. That is
sufficient at one episode per day; if parallel rendering is ever needed, the
job-runner interface is the seam to put Redis behind.

## The two halves

**Deterministic**: filters, scoring, verification comparison, claim
construction from structured fields, price/status injection, scene planning,
FFmpeg graph construction, every QA check, publication packaging.

**Model-assisted**: candidate explanation, research-query planning,
source-to-claim extraction, script prose, title and description wording.

The boundary is enforced, not stylistic. A model can influence *how something
is said and what order it comes in*. It cannot introduce a number, a date, a
name, a status or a historical assertion, because the fact validator parses the
finished script back into atomic claims and matches each against the bundle.

## Data flow

```
ListingObservation      one source's read of one listing at one time
  → PropertyRecord      canonical, deduplicated, with a verification result
  → Candidate           scored, ranked, with a plain-language rationale
  → Claim               atomic fact + sources + confidence + scope
  → EvidenceBundle      the writer's entire world
  → Script              segments + evidence refs + on-screen text
  → NarrationSegment    one audio file per segment
  → ScenePlan           what is on screen, when, for how long
  → QAReport            typed findings, grouped into repair tasks
  → PublicationPackage  the §18 directory
```

Each arrow is a stage. Each stage writes one artifact to the run directory, so
any stage can be re-run from the artifact its predecessor left behind — no
network re-acquisition required.

## Interfaces that keep providers replaceable

- `ListingSourceAdapter` — Zillow today; Realtor.com, Redfin, a brokerage feed
  or a saved-search export drop in without touching the pipeline.
- `VerificationAdapter` — resolution ladder: canonical link, MLS search,
  address + brokerage, allow-listed brokerage domains, alternate portal. Each
  step down lowers the verification confidence.
- `GenerationProvider` — Ollama with JSON-schema-constrained output, a
  deterministic evidence-driven writer, and a fallback chain between them.
- `SpeechEngine` — Kokoro, or a silent engine that produces correctly-timed
  silence for timing inspection (and that QA refuses to publish).
- `Publisher` — YouTube resumable upload, or a dry-run publisher that writes
  the request to disk.

None of these is imported at module scope in a way that makes it a hard
dependency; `mpvf doctor` reports which are actually present.

## Why no Remotion

Automated rendering under Remotion's licence can create a paid monthly
obligation. The renderer is FFmpeg plus deterministic SVG templates rasterized
by CairoSVG, `rsvg-convert` or Inkscape — whichever is installed. Remotion
remains a documented alternative if an operator chooses that licence.

## Reproducibility

Every render stores the exact command, the input hash over asset checksums, the
scene-plan hash, the resolved settings, and the FFmpeg version. Every model
generation stores provider, model, prompt id and version, schema version,
temperature, input hash and raw output. Raw output is kept for debugging and is
never allowed to bypass schema validation.
