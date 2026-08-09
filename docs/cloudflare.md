# Running MPVF on Cloudflare

All inference goes to Cloudflare Workers AI or OpenRouter. All hosting is
Cloudflare. This document is specific about where the boundary falls, because
one part of this pipeline genuinely cannot run in a Workers isolate.

---

## What runs where, and why

| Tier | Platform | Why |
|---|---|---|
| Scheduling, queueing, dashboard | **Worker** + Cron Triggers + Queues | Small, fast, event-shaped |
| Control-plane state | **D1** | The dashboard reads it without waking a container |
| Artifacts, masters, thumbnails | **R2** | Durable; the compute node is disposable |
| Script, research, titles | **Workers AI** → **OpenRouter** → deterministic | Hosted inference |
| Narration, transcription | **Workers AI** | Hosted inference |
| Discovery, assets, render | **Container** | See below |

### Why the render tier is a Container, not a Worker

A Workers isolate has no native binaries, no writable filesystem, and a CPU
ceiling measured in minutes. One MPVF episode:

- drives a real browser for discovery (Playwright/Chromium),
- decodes and perceptually hashes 100+ JPEGs (Pillow),
- rasterizes SVG overlays (Cairo or rsvg),
- runs a multi-input FFmpeg filter graph to encode seven minutes of 1080p.

None of that is a Worker workload, and pretending otherwise would produce a
deployment that fails on its first real run. It goes in a Cloudflare Container,
which is still Cloudflare hosting — a Worker starts it, waits, and shuts it
down. The Worker stays the control plane.

If Containers are not enabled on the account, the same image runs anywhere that
takes a Dockerfile while the Worker, D1, R2, Queues and all inference stay on
Cloudflare.

---

## Inference

### Chain

```
Workers AI  →  OpenRouter  →  deterministic writer
```

Configured in `config/settings.yaml`:

```yaml
provider:
  kind: cloudflare
  model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
  secondary_kind: openrouter
  secondary_model: anthropic/claude-3.5-haiku
  fallback: deterministic
  require_schema_support: true
```

Both back-ends are driven through their OpenAI-compatible chat endpoint, so
there is one transport, one retry policy and one schema validator:

- Workers AI — `POST /client/v4/accounts/{account_id}/ai/v1/chat/completions`
- OpenRouter — `POST https://openrouter.ai/api/v1/chat/completions`

Every request carries `response_format: {type: "json_schema", strict: true}`
built from the Pydantic model. A response that misses the schema is retried
**with the validation errors included**, then fails — model output never
bypasses validation (§12.2).

`require_schema_support: true` sends OpenRouter `provider.require_parameters`,
so a request cannot silently land on an upstream that ignores the schema.

**Every link is optional.** A missing credential drops that provider from the
chain with a warning; a 5xx falls through to the next; the deterministic writer
is the floor. An inference outage produces a plainer episode, not a failed run.

### Speech and transcription

```yaml
speech:
  engine: cloudflare
  model: "@cf/myshell-ai/melotts"
  aligner: cloudflare
  alignment_model: "@cf/openai/whisper-large-v3-turbo"
```

These use the native `/ai/run/{model}` endpoint — Workers AI has no
OpenAI-shaped audio surface. Two details worth knowing:

- Workers AI returns MP3; the pipeline treats narration segments as WAV
  (captions, concatenation and loudness all assume it). The engine transcodes
  once, at the boundary, and reports the *measured* duration instead of an
  estimate — scene timing is built from that number.
- Transcription finally makes the §13.4 gate real: the narration is transcribed
  and compared to the approved script at 97% similarity. Without a transcriber
  the gate is skipped and `mpvf doctor` says so.

### Model ids are configuration

Model names live in `config/settings.yaml` (§24). Swapping
`@cf/meta/llama-3.3-70b-instruct-fp8-fast` for anything else is a config edit.

---

## Credentials

Never in a config file, never in the database, never in a prompt (§16). Read
from the environment first, then from files in `secrets/` (mode 700):

| Secret | Env var | File |
|---|---|---|
| Account id | `MPVF_CLOUDFLARE_ACCOUNT_ID` | `secrets/cloudflare-account-id` |
| API token | `MPVF_CLOUDFLARE_API_TOKEN` | `secrets/cloudflare-api-token` |
| OpenRouter key | `MPVF_OPENROUTER_API_KEY` | `secrets/openrouter-api-key` |
| R2 access key | `MPVF_R2_ACCESS_KEY_ID` | `secrets/r2-access-key-id` |
| R2 secret | `MPVF_R2_SECRET_ACCESS_KEY` | `secrets/r2-secret-access-key` |

The API token needs **Workers AI: Read** and, for the storage tier, **D1: Edit**
and R2 object read/write.

`mpvf doctor` reports which secrets are present and which chain will actually
run. It never prints a value:

```
✓ cloud credentials   3 secrets present
✓ model provider      cloudflare:@cf/meta/llama-3.3-70b-instruct-fp8-fast
                      -> openrouter:anthropic/claude-3.5-haiku
                      -> deterministic:deterministic-v1
✓ tts                 workers-ai (@cf/myshell-ai/melotts)
✓ transcript check    workers-ai @cf/openai/whisper-large-v3-turbo
```

---

## Storage

```yaml
storage:
  artifacts: r2
  r2_bucket: mpvf-artifacts
  database: d1
  d1_database_id: "..."
```

**R2** holds run artifacts at `runs/{run_id}/{stage}/{file}`. Stages still write
real files locally — FFmpeg needs them — and the directory is mirrored at the
run boundary. `include_bulk=False` skips `pages/` and `assets/`, which are large
and re-derivable; masters, scripts, evidence and manifests always go up.

**D1** holds the control plane — runs, stages, publications — so the dashboard
Worker can serve without waking a container.

The full relational store stays on SQLite inside the container. That is a
deliberate limit: the pipeline uses SQLAlchemy sessions, transactions and joins
that a per-statement HTTP API serves badly. Porting the whole schema to D1 is a
real piece of work and is **not** done here.

---

## Deploying

```bash
cd deploy
npm install

wrangler d1 create mpvf                    # put the id in wrangler.toml
wrangler r2 bucket create mpvf-artifacts
wrangler queues create mpvf-episodes

wrangler secret put MPVF_CLOUDFLARE_API_TOKEN
wrangler secret put MPVF_OPENROUTER_API_KEY
wrangler secret put MPVF_R2_ACCESS_KEY_ID
wrangler secret put MPVF_R2_SECRET_ACCESS_KEY

wrangler deploy                            # builds the container image too
```

The cron in `wrangler.toml` is UTC. `0 9 * * *` is 05:00 EDT; shift to
`0 10 * * *` for EST, since Cloudflare cron has no timezone.

### Flow

```
Cron (09:00 UTC)
  → Worker.scheduled: pick today's template, enqueue
  → Queue → Worker.queue: start the container, hold the connection
      → container: discover → … → render → qa → publish
          inference  → Workers AI / OpenRouter
          artifacts  → R2
          control    → D1
  → Worker.fetch: dashboard reads D1 + R2
```

A queue failure retries twice. That is safe because stage state is persisted
and completed stages reuse their artifacts rather than re-acquiring. A **skip**
is acked, not retried — publishing nothing is a valid outcome (§2.2), not an
error to recover from.

---

## Cost shape

Per episode, roughly: one long-context script generation plus a handful of
small calls (titles, research extraction), ~1,000 words of TTS, one whisper
pass over ~7 minutes, ~2 GB into R2, and tens of minutes of container time.
Container time dominates — encoding is the expensive part, not inference.

To cut it: lower `render.video_crf` quality, drop `preview_height`, or set
`storage.artifacts: r2` with `include_bulk=False` so raw pages and unused
images never leave the node.

---

## Verification status

The provider request shapes here are asserted by tests against a mock
transport — URL, auth header, `response_format` block, OpenRouter routing
flags, retry and fallback behaviour. Those tests pin *our* side of the
contract.

They do not prove the vendor still accepts that shape. The Cloudflare and
OpenRouter documentation sites were unreachable from the environment this was
written in, so the request formats come from their documented contracts without
a live re-check. The first real call will confirm or refute it immediately, and
a mismatch surfaces as a clear API error rather than silent bad output. Audio
response parsing is deliberately tolerant of several payload shapes for the
same reason.
