# Operator guide

## Daily rhythm

Default execution window (all configurable):

| Time | Stage |
|---|---|
| 05:00 | discovery and verification |
| 06:00 | assets and research |
| 07:00 | script and narration |
| 08:00 | render and QA |
| 09:00 | private upload or review notification |
| 17:00 | configured public release time |

## Publishing modes

1. **review** — stops after the script and candidate assembly.
2. **private_upload** — renders and uploads privately; you publish by hand.
3. **scheduled** — uploads and schedules after QA passes.
4. **full_auto** — end to end unless a gate fails.

**Keep `private_upload` until the twenty-run pilot passes.** `full_auto` is
opt-in configuration, never a default.

## Reading a run

`mpvf status` lists recent runs. `mpvf status <run-id>` adds per-stage state and
duration. The dashboard's Today page shows the current run, the stage strip,
blockers and the next action; every failed stage has a saved artifact and a
copy-pasteable re-run command.

## When a run fails or skips

A **skip** is a success of the quality system, not an incident. Common ones:

| Code | Meaning | What to do |
|---|---|---|
| `lineup_below_quality_floor` | today's listings are weak | nothing; or loosen the template deliberately |
| `editorial_gate` | the script is repetitive or hype-heavy | read the subscores on the Script page |
| `insufficient_candidates` | fewer than five eligible listings | let the fallback template run, or skip |

Failures worth acting on:

| Code | Meaning | What to do |
|---|---|---|
| `selector_failure` | a source changed layout | update `selectors_v1.py`, add the saved page as a fixture |
| `access_challenge` | the source presented a challenge | do **not** try to bypass it; use a fallback source |
| `evidence_gaps` | a property lacks price, status or credit | check the Evidence page; usually a verification miss |
| `script_facts_unsupported` | prose asserted something unevidenced | the listed claims show exactly what |
| `listing_changed` | status or price moved before publication | replace the property or hold the episode |

## Re-running one stage

```bash
mpvf run <template> --run-id <run-id> --from-stage script --to-stage qa
```

Upstream network acquisition is not repeated; each stage reads its predecessor's
artifact.

## Pronunciation

`config/pronunciation.yaml` ships with the Maine names that trip up any TTS
voice (Calais, Machias, Damariscotta, Carrabassett, Mooselookmeguntic…). Terms
marked `watchlist: true` are surfaced for a listen during the pilot. Add an
entry the first time you hear a name mangled; it applies from the next run.

## YouTube setup

```bash
mkdir -p secrets   # mode 700, never committed
# place youtube-client-secret.json in secrets/
mpvf youtube auth
mpvf youtube state
```

A new, unaudited API project has uploads restricted to private and cannot make
them public through the API. That is a Google policy, not a bug: request the
audit, and until it clears run in `private_upload` mode. The dashboard surfaces
this rather than retrying forever.

## Retention

```bash
mpvf cleanup --older-than 90d --dry-run   # always dry-run first
mpvf cleanup --older-than 90d --execute
```

Raw pages and unused images: 90 days. Preview renders: 30 days. Database
records, evidence bundles, scripts, manifests, masters, thumbnails and
publication metadata: kept indefinitely. Published masters are protected unless
you explicitly configure otherwise, and every destructive action is written to
the append-only event stream.

## The ten-episode pilot

Build and privately upload ten real episodes across at least five templates.
Record, per episode: total run time, manual correction time, source failures,
factual corrections, pronunciation corrections, script-quality score, visual
defects, and the YouTube processing outcome.

Do not enable automatic public publishing until the §20 acceptance criteria pass:
twenty consecutive runs that each complete, skip cleanly, or fail with an
actionable reason, and under fifteen minutes of routine review per episode.

## Security

Dashboard binds to localhost. Secrets live in `secrets/` at mode 700 and never
in the database or a config file. Acquisition uses a dedicated browser profile,
not your everyday one. Downloads are size-capped, MIME-validated and decoded
under a pixel limit; acquired filenames are sanitized; nothing acquired is ever
executed.
