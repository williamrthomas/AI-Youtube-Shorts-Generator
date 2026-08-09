"""R2 artifact mirroring and the D1 control-plane client, all against fakes."""

from __future__ import annotations

import json

import httpx
import pytest

from mpvf.pipeline.artifacts import ArtifactStore
from mpvf.storage.d1 import CONTROL_PLANE_SCHEMA, D1Client, D1Config, D1Error, build_client
from mpvf.storage.r2 import R2ArtifactStore, R2Config, R2Unavailable, build_store


class FakeS3:
    """Minimal in-memory stand-in for the S3 surface R2 exposes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803 - boto3 signature
        with open(filename, "rb") as handle:
            self.objects[f"{bucket}/{key}"] = handle.read()
        self.content_types[key] = (ExtraArgs or {}).get("ContentType", "")

    def download_file(self, bucket, key, filename):
        with open(filename, "wb") as handle:
            handle.write(self.objects[f"{bucket}/{key}"])

    def get_paginator(self, _name):
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3 signature
                contents = [
                    {"Key": key.split("/", 1)[1]}
                    for key in outer.objects
                    if key.startswith(f"{Bucket}/{Prefix}")
                ]
                yield {"Contents": contents}

        return Paginator()


@pytest.fixture
def r2_config() -> R2Config:
    return R2Config(
        bucket="mpvf-artifacts",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="key",
        secret_access_key="secret",
        prefix="runs",
    )


@pytest.fixture
def populated_store(tmp_path) -> ArtifactStore:
    store = ArtifactStore.for_run(tmp_path / "runs", "run_1", "coastal")
    store.write_json("evidence", "bundle.json", {"claims": []})
    store.write_text("script", "script.md", "# Episode")
    store.write_bytes("render", "master.mp4", b"video-bytes")
    store.write_text("pages", "saved.html", "<html></html>")
    store.write_bytes("assets", "image.jpg", b"jpeg-bytes")
    return store


class TestR2Mirroring:
    def test_every_file_is_uploaded_under_the_run_prefix(self, r2_config, populated_store):
        fake = FakeS3()
        result = R2ArtifactStore(r2_config, client=fake).mirror(populated_store, "run_1")

        assert result.count == 5
        assert "mpvf-artifacts/runs/run_1/evidence/bundle.json" in fake.objects
        assert "mpvf-artifacts/runs/run_1/render/master.mp4" in fake.objects
        assert result.bytes_uploaded > 0

    def test_content_types_are_set(self, r2_config, populated_store):
        fake = FakeS3()
        R2ArtifactStore(r2_config, client=fake).mirror(populated_store, "run_1")
        assert fake.content_types["runs/run_1/render/master.mp4"] == "video/mp4"
        assert fake.content_types["runs/run_1/evidence/bundle.json"] == "application/json"

    def test_bulk_directories_can_be_skipped(self, r2_config, populated_store):
        """Raw pages and downloaded images are re-derivable; masters are not."""

        fake = FakeS3()
        result = R2ArtifactStore(r2_config, client=fake).mirror(
            populated_store, "run_1", include_bulk=False
        )
        uploaded = " ".join(result.uploaded)
        assert "render/master.mp4" in uploaded
        assert "pages/" not in uploaded
        assert "assets/" not in uploaded
        assert len(result.skipped) == 2

    def test_a_run_round_trips(self, r2_config, populated_store, tmp_path):
        fake = FakeS3()
        store = R2ArtifactStore(r2_config, client=fake)
        store.mirror(populated_store, "run_1")

        restored_store = ArtifactStore(tmp_path / "restored")
        restored_store.initialize()
        restored = store.restore(restored_store, "run_1")

        assert len(restored) == 5
        assert (restored_store.root / "render" / "master.mp4").read_bytes() == b"video-bytes"
        assert restored_store.read_json("evidence", "bundle.json") == {"claims": []}

    def test_listing_a_run(self, r2_config, populated_store):
        fake = FakeS3()
        store = R2ArtifactStore(r2_config, client=fake)
        store.mirror(populated_store, "run_1")
        assert len(store.list_run("run_1")) == 5

    def test_public_url_shape(self, r2_config):
        url = R2ArtifactStore(r2_config, client=FakeS3()).public_url("run_1", "render/master.mp4")
        assert url == (
            "https://acct.r2.cloudflarestorage.com/mpvf-artifacts/runs/run_1/render/master.mp4"
        )

    def test_incomplete_config_is_reported(self):
        config = R2Config(bucket="b", endpoint_url="", access_key_id="", secret_access_key="")
        assert not config.complete()
        assert config.missing() == ["access_key_id", "endpoint_url", "secret_access_key"]
        with pytest.raises(R2Unavailable):
            R2ArtifactStore(config).mirror(ArtifactStore("/tmp/nothing"), "run_1")

    def test_build_store_returns_none_for_local_storage(self, settings):
        assert build_store(settings) is None

    def test_build_store_warns_and_returns_none_when_unconfigured(self, settings):
        settings.storage.artifacts = "r2"
        assert build_store(settings) is None


class TestD1:
    def _client(self, handler) -> D1Client:
        return D1Client(
            D1Config(account_id="acct", database_id="db", api_token="tok"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_query_url_shape(self):
        config = D1Config(account_id="acct", database_id="db", api_token="t")
        assert config.query_url.endswith("/accounts/acct/d1/database/db/query")

    def test_rows_are_returned(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["sql"].startswith("SELECT")
            assert body["params"] == [5]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"results": [{"id": "run_1", "state": "archived"}]}],
                },
            )

        rows = self._client(handler).query("SELECT * FROM runs LIMIT ?", [5])
        assert rows == [{"id": "run_1", "state": "archived"}]

    def test_an_unsuccessful_envelope_raises(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": False, "errors": [{"message": "no table"}]})

        with pytest.raises(D1Error, match="no table"):
            self._client(handler).query("SELECT 1")

    def test_an_http_error_raises(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        with pytest.raises(D1Error, match="403"):
            self._client(handler).query("SELECT 1")

    def test_unconfigured_client_refuses_to_query(self):
        client = D1Client(D1Config(account_id="", database_id="", api_token=""))
        assert not client.available()
        with pytest.raises(D1Error, match="not configured"):
            client.query("SELECT 1")

    def test_schema_script_runs_each_statement(self):
        statements: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            statements.append(json.loads(request.content)["sql"])
            return httpx.Response(200, json={"success": True, "result": []})

        count = self._client(handler).ensure_schema()
        assert count == len(statements) > 3
        assert any("CREATE TABLE IF NOT EXISTS runs" in sql for sql in statements)
        assert all(";" not in sql for sql in statements)

    def test_upsert_run_is_idempotent_sql(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": []})

        self._client(handler).upsert_run(
            {
                "id": "run_1",
                "template_slug": "coastal",
                "state": "archived",
                "mode": "private_upload",
                "created_at": "2026-08-09T05:00:00Z",
            }
        )
        assert "ON CONFLICT(id) DO UPDATE" in seen[0]["sql"]
        assert seen[0]["params"][0] == "run_1"

    def test_non_scalar_parameters_are_stringified(self):
        from datetime import UTC, datetime

        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": []})

        stamp = datetime(2026, 8, 9, tzinfo=UTC)
        self._client(handler).query("INSERT INTO runs VALUES (?)", [stamp])
        assert isinstance(seen[0]["params"][0], str)

    def test_control_plane_schema_covers_the_dashboard_tables(self):
        for table in ("runs", "run_stages", "publications"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in CONTROL_PLANE_SCHEMA

    def test_build_client_returns_none_for_sqlite(self, settings):
        assert build_client(settings) is None
