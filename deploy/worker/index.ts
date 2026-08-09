/**
 * MPVF control plane.
 *
 * The Worker decides *what* to run and serves the dashboard; the container
 * does the work. Nothing here renders video or drives a browser: a Workers
 * isolate has no native binaries and a hard CPU ceiling, and one episode is
 * tens of minutes of FFmpeg.
 */

import { Container, getContainer } from '@cloudflare/containers';

interface Env {
  DB: D1Database;
  ARTIFACTS: R2Bucket;
  EPISODES: Queue<EpisodeMessage>;
  EPISODE_RUNNER: DurableObjectNamespace<EpisodeRunner>;
  MPVF_TEMPLATES: string;
  MPVF_PUBLISHING_MODE: string;
}

interface EpisodeMessage {
  template: string;
  mode: string;
  scheduledFor: string;
}

/** The render/acquisition tier, held open for the length of a run. */
export class EpisodeRunner extends Container<Env> {
  defaultPort = 8080;
  // An episode is long: discovery, research, narration, then a 1080p encode.
  sleepAfter = '90m';
}

/** Today's template, from the weekly editorial calendar (§5.3). */
function templateForToday(env: Env, now: Date): string {
  const templates = env.MPVF_TEMPLATES.split(',').map((slug) => slug.trim()).filter(Boolean);
  if (templates.length === 0) throw new Error('MPVF_TEMPLATES is empty');
  return templates[now.getUTCDay() % templates.length];
}

export default {
  /** Cron trigger: enqueue, do not run inline. */
  async scheduled(event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    const now = new Date(event.scheduledTime);
    const message: EpisodeMessage = {
      template: templateForToday(env, now),
      mode: env.MPVF_PUBLISHING_MODE || 'private_upload',
      scheduledFor: now.toISOString(),
    };
    ctx.waitUntil(env.EPISODES.send(message));
  },

  /**
   * Queue consumer: hand the episode to a container and wait.
   *
   * A failure is retried by the queue, and the pipeline's own stage state
   * makes a repeat safe — completed stages reuse their artifacts.
   */
  async queue(batch: MessageBatch<EpisodeMessage>, env: Env): Promise<void> {
    for (const message of batch.messages) {
      const { template, mode } = message.body;
      try {
        const container = getContainer(env.EPISODE_RUNNER, `episode-${template}`);
        const response = await container.fetch(
          new Request('http://container/run', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ template, mode }),
          }),
        );
        if (!response.ok) {
          console.error(`episode ${template} failed: ${response.status} ${await response.text()}`);
          message.retry();
          continue;
        }
        const summary = (await response.json()) as { outcome?: string; run_id?: string };
        // A skip is a valid outcome, not a failure: do not retry it (§2.2).
        console.log(`episode ${template}: ${summary.outcome} (${summary.run_id})`);
        message.ack();
      } catch (error) {
        console.error(`episode ${template} threw: ${error}`);
        message.retry();
      }
    }
  },

  /** Read-only dashboard over the D1 control plane and R2 artifacts. */
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === '/api/runs') {
      const { results } = await env.DB.prepare(
        'SELECT id, template_slug, state, mode, created_at, failure_code FROM runs ' +
          'ORDER BY created_at DESC LIMIT 100',
      ).all();
      return Response.json(results);
    }

    if (url.pathname.startsWith('/artifacts/')) {
      const key = url.pathname.slice('/artifacts/'.length);
      const object = await env.ARTIFACTS.get(key);
      if (!object) return new Response('not found', { status: 404 });
      const headers = new Headers();
      object.writeHttpMetadata(headers);
      headers.set('etag', object.httpEtag);
      return new Response(object.body, { headers });
    }

    if (url.pathname === '/api/health') {
      const row = await env.DB.prepare('SELECT COUNT(*) AS runs FROM runs').first<{ runs: number }>();
      return Response.json({ ok: true, runs: row?.runs ?? 0 });
    }

    return new Response('mpvf control plane', { status: 200 });
  },
};
