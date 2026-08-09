"""Container entrypoint: run one episode, mirror it to R2, publish the state.

The Worker POSTs ``{"template": "...", "mode": "..."}`` and holds the
connection until the run finishes. Everything durable leaves through R2 and
D1, so the container can be discarded afterwards.
"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from mpvf.config.settings import load_settings
from mpvf.generation.cloud import CloudCredentials
from mpvf.models import migrations
from mpvf.observability.logging import configure_logging, get_logger
from mpvf.pipeline.artifacts import ArtifactStore
from mpvf.pipeline.runner import Runner
from mpvf.storage.d1 import build_client
from mpvf.storage.r2 import build_store

logger = get_logger("container")


def run_episode(payload: dict) -> dict:
    settings = load_settings(os.environ.get("MPVF_CONFIG"))
    settings.ensure_directories()
    migrations.upgrade(settings)

    credentials = CloudCredentials.load(settings.secrets_dir)
    runner = Runner(settings)

    summary = runner.run_episode(
        payload["template"],
        mode=payload.get("mode", settings.publishing_mode),
    )

    # Durability: the container is ephemeral, the bucket is not.
    store = build_store(settings, credentials)
    if store is not None and summary.get("artifact_dir"):
        mirror = store.mirror(
            ArtifactStore(summary["artifact_dir"]),
            summary["run_id"],
            include_bulk=payload.get("mirror_bulk", False),
        )
        summary["r2_objects"] = mirror.count

    d1 = build_client(settings, credentials)
    if d1 is not None:
        d1.ensure_schema()
        for item in runner.status(summary.get("run_id")):
            d1.upsert_run(
                {
                    "id": item["run_id"],
                    "template_slug": item["template"],
                    "state": item["state"],
                    "mode": item["mode"],
                    "created_at": str(item["created_at"]),
                    "completed_at": str(item["completed_at"]) if item["completed_at"] else None,
                    "failure_code": item["failure_code"],
                    "failure_detail": item["failure_detail"],
                    "artifact_prefix": f"runs/{item['run_id']}",
                }
            )
            for stage in item["stages"]:
                d1.upsert_stage(item["run_id"], stage)
    return summary


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        self._respond(200, {"ok": True})

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        length = int(self.headers.get("content-length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return
        if not payload.get("template"):
            self._respond(400, {"error": "template is required"})
            return

        try:
            self._respond(200, run_episode(payload))
        except Exception as exc:  # noqa: BLE001 - report, never crash the container
            logger.error("episode failed", extra={"detail": {"error": str(exc)}})
            self._respond(500, {"error": str(exc), "traceback": traceback.format_exc()[-2000:]})

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        logger.info("http", extra={"detail": {"message": fmt % args}})


if __name__ == "__main__":
    configure_logging()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("container ready", extra={"detail": {"port": port}})
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
