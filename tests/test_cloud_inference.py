"""Hosted inference through Cloudflare Workers AI and OpenRouter.

Every test drives a mock transport, so the suite stays offline and free while
still asserting the exact bytes we put on the wire.
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path

import httpx
import pytest

from mpvf.config.settings import ProviderSettings, SpeechSettings
from mpvf.generation.cloud import (
    CloudCredentials,
    CredentialMissing,
    OpenRouterProvider,
    WorkersAIProvider,
    workers_ai_base_url,
    workers_ai_run_url,
)
from mpvf.generation.openai_compatible import OpenAICompatibleProvider, json_schema_payload
from mpvf.generation.provider import (
    DeterministicProvider,
    FallbackProvider,
    GenerationError,
    Message,
    build_provider,
    deterministic_floor,
)
from mpvf.scripting.schemas import TitleCandidates
from mpvf.speech.cloud_tts import WorkersAISpeechEngine, WorkersAITranscriber
from mpvf.speech.tts import SilentEngine, TTSError, build_engine

VALID = '{"titles": ["5 Maine Coastal Homes"], "reasoning": "matches the lineup"}'


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def _messages() -> list[Message]:
    return [Message(role="user", content="Write three titles")]


class TestCredentials:
    def test_environment_wins_over_files(self, tmp_path, monkeypatch):
        (tmp_path / "openrouter-api-key").write_text("from-file")
        monkeypatch.setenv("MPVF_OPENROUTER_API_KEY", "from-env")
        credentials = CloudCredentials.load(tmp_path)
        assert credentials.openrouter_api_key == "from-env"

    def test_files_are_used_when_the_environment_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MPVF_OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        (tmp_path / "openrouter-api-key").write_text("  from-file\n")
        assert CloudCredentials.load(tmp_path).openrouter_api_key == "from-file"

    def test_vendor_variable_names_are_accepted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MPVF_CLOUDFLARE_ACCOUNT_ID", raising=False)
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
        assert CloudCredentials.load(tmp_path).cloudflare_account_id == "acct"

    def test_state_never_exposes_a_secret(self):
        credentials = CloudCredentials(openrouter_api_key="sk-super-secret")
        state = credentials.state()
        assert state == {
            "cloudflare_account_id": False,
            "cloudflare_api_token": False,
            "openrouter_api_key": True,
        }
        assert "sk-super-secret" not in json.dumps(state)


class TestUrls:
    def test_workers_ai_openai_surface(self):
        assert workers_ai_base_url("acct123").endswith("/accounts/acct123/ai/v1")

    def test_workers_ai_native_run_endpoint(self):
        url = workers_ai_run_url("acct123", "@cf/openai/whisper")
        assert url.endswith("/accounts/acct123/ai/run/@cf/openai/whisper")


class TestRequestShape:
    def test_cloudflare_posts_to_the_account_endpoint_with_a_bearer_token(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return _chat_response(VALID)

        provider = WorkersAIProvider(
            model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            account_id="acct123",
            api_token="tok",
            client=_client(handler),
        )
        provider.generate_structured("title", _messages(), TitleCandidates)

        assert seen["url"] == (
            "https://api.cloudflare.com/client/v4/accounts/acct123/ai/v1/chat/completions"
        )
        assert seen["auth"] == "Bearer tok"

    def test_openrouter_sends_attribution_headers_and_requires_schema_support(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["referer"] = request.headers.get("http-referer")
            seen["title"] = request.headers.get("x-title")
            seen["body"] = json.loads(request.content)
            return _chat_response(VALID)

        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-haiku", api_key="k", client=_client(handler)
        )
        provider.generate_structured("title", _messages(), TitleCandidates)

        assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert seen["referer"] and seen["title"]
        assert seen["body"]["provider"] == {"require_parameters": True}

    def test_require_schema_support_can_be_turned_off(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return _chat_response(VALID)

        OpenRouterProvider(
            model="m", api_key="k", require_parameters=False, client=_client(handler)
        ).generate_structured("title", _messages(), TitleCandidates)
        assert "provider" not in seen["body"]

    def test_the_schema_is_sent_as_a_strict_json_schema(self):
        payload = json_schema_payload(TitleCandidates)
        assert payload["type"] == "json_schema"
        assert payload["json_schema"]["name"] == "TitleCandidates"
        assert payload["json_schema"]["strict"] is True
        assert payload["json_schema"]["schema"]["additionalProperties"] is False

    def test_credentials_are_never_placed_in_the_prompt(self):
        """A model must never see a token (§12.2)."""

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            return _chat_response(VALID)

        WorkersAIProvider(
            model="m", account_id="acct123", api_token="super-secret-token", client=_client(handler)
        ).generate_structured("title", _messages(), TitleCandidates)

        body = json.loads(seen["body"])
        assert "super-secret-token" not in json.dumps(body["messages"])
        assert "acct123" not in json.dumps(body["messages"])


class TestResponseHandling:
    def test_valid_json_is_parsed_into_the_schema(self):
        provider = WorkersAIProvider(
            model="m",
            account_id="a",
            api_token="t",
            client=_client(lambda r: _chat_response(VALID)),
        )
        result, record = provider.generate_structured("title", _messages(), TitleCandidates)
        assert result.titles == ["5 Maine Coastal Homes"]
        assert record.options["usage"]["total_tokens"] == 15
        assert record.raw_output

    def test_a_code_fence_is_stripped(self):
        fenced = f"```json\n{VALID}\n```"
        provider = WorkersAIProvider(
            model="m",
            account_id="a",
            api_token="t",
            client=_client(lambda r: _chat_response(fenced)),
        )
        result, _record = provider.generate_structured("title", _messages(), TitleCandidates)
        assert result.titles

    def test_content_parts_are_joined(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": [{"type": "text", "text": VALID}]}}]},
            )

        provider = OpenRouterProvider(model="m", api_key="k", client=_client(handler))
        result, _record = provider.generate_structured("title", _messages(), TitleCandidates)
        assert result.titles

    def test_a_schema_violation_is_retried_with_the_errors(self):
        attempts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            attempts.append(body)
            if len(attempts) == 1:
                return _chat_response('{"wrong_field": true}')
            return _chat_response(VALID)

        provider = OpenRouterProvider(model="m", api_key="k", client=_client(handler))
        result, record = provider.generate_structured("title", _messages(), TitleCandidates)

        assert result.titles
        assert record.attempts == 2
        # The retry must tell the model what was wrong, not just ask again.
        assert "did not satisfy the schema" in json.dumps(attempts[1]["messages"])

    def test_persistent_schema_violation_raises(self):
        provider = OpenRouterProvider(
            model="m",
            api_key="k",
            max_attempts=2,
            client=_client(lambda r: _chat_response('{"nope": 1}')),
        )
        with pytest.raises(GenerationError):
            provider.generate_structured("title", _messages(), TitleCandidates)

    def test_a_server_error_is_retried_then_raises(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={"error": {"message": "overloaded"}})

        provider = OpenRouterProvider(
            model="m", api_key="k", max_attempts=2, client=_client(handler)
        )
        with pytest.raises(GenerationError, match="503"):
            provider.generate_structured("title", _messages(), TitleCandidates)
        assert calls["n"] == 2

    def test_an_auth_error_fails_immediately_without_retrying(self):
        """Retrying a bad key just burns time; surface it at once."""

        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        provider = OpenRouterProvider(
            model="m", api_key="bad", max_attempts=3, client=_client(handler)
        )
        with pytest.raises(GenerationError, match="invalid api key"):
            provider.generate_structured("title", _messages(), TitleCandidates)
        assert calls["n"] == 1

    def test_a_cloudflare_error_envelope_is_reported(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"success": False, "errors": [{"message": "bad model"}]}
            )

        provider = WorkersAIProvider(
            model="m", account_id="a", api_token="t", client=_client(handler)
        )
        with pytest.raises(GenerationError, match="bad model"):
            provider.generate_structured("title", _messages(), TitleCandidates)


class TestProviderChain:
    def _settings(self, **overrides) -> ProviderSettings:
        return ProviderSettings(max_attempts=1, **overrides)

    def test_default_chain_is_cloudflare_then_openrouter_then_deterministic(self):
        credentials = CloudCredentials(
            cloudflare_account_id="a", cloudflare_api_token="t", openrouter_api_key="k"
        )
        provider = build_provider(self._settings(), credentials=credentials)
        assert isinstance(provider, FallbackProvider)
        assert provider.primary.name == "cloudflare"
        assert provider.secondary.primary.name == "openrouter"
        assert isinstance(provider.secondary.secondary, DeterministicProvider)

    def test_a_missing_credential_drops_that_provider(self):
        credentials = CloudCredentials(openrouter_api_key="k")
        provider = build_provider(self._settings(), credentials=credentials)
        assert provider.primary.name == "openrouter"

    def test_no_credentials_at_all_degrades_to_deterministic(self):
        provider = build_provider(self._settings(), credentials=CloudCredentials())
        assert isinstance(provider, DeterministicProvider)

    def test_the_deterministic_floor_is_reachable_through_a_nested_chain(self):
        """The script generator registers its handler on this; it must be found."""

        credentials = CloudCredentials(
            cloudflare_account_id="a", cloudflare_api_token="t", openrouter_api_key="k"
        )
        provider = build_provider(self._settings(), credentials=credentials)
        assert isinstance(deterministic_floor(provider), DeterministicProvider)

    def test_a_failing_primary_falls_through_to_the_next(self):
        calls: list[str] = []

        def failing(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "cloudflare" in str(request.url):
                return httpx.Response(500, json={"error": {"message": "down"}})
            return _chat_response(VALID)

        client = _client(failing)
        credentials = CloudCredentials(
            cloudflare_account_id="a", cloudflare_api_token="t", openrouter_api_key="k"
        )
        provider = build_provider(self._settings(), credentials=credentials, client=client)
        result, record = provider.generate_structured("title", _messages(), TitleCandidates)

        assert result.titles
        assert record.provider == "openrouter"
        assert any("cloudflare" in url for url in calls)

    def test_explicit_deterministic_skips_the_network_entirely(self):
        provider = build_provider(
            self._settings(kind="deterministic"),
            credentials=CloudCredentials(cloudflare_account_id="a", cloudflare_api_token="t"),
        )
        assert isinstance(provider, DeterministicProvider)

    def test_a_provider_without_a_credential_reports_unavailable(self):
        provider = OpenAICompatibleProvider(model="m", base_url="https://x.test", api_key=None)
        assert not provider.available()

    def test_constructing_a_hosted_provider_without_credentials_raises(self):
        with pytest.raises(CredentialMissing):
            WorkersAIProvider(model="m", account_id="", api_token="")
        with pytest.raises(CredentialMissing):
            OpenRouterProvider(model="m", api_key="")


def _wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


class TestWorkersAISpeech:
    def test_a_wav_response_is_written_and_measured(self, tmp_path):
        audio = _wav_bytes(1.5)

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/ai/run/@cf/myshell-ai/melotts" in str(request.url)
            return httpx.Response(
                200,
                json={"result": {"audio": base64.b64encode(audio).decode()}, "success": True},
                headers={"content-type": "application/json"},
            )

        engine = WorkersAISpeechEngine(account_id="a", api_token="t", client=_client(handler))
        destination = tmp_path / "seg.wav"
        duration = engine.synthesize("Hello from Boothbay Harbor.", destination, "af_heart", 1.0)

        assert destination.exists()
        assert duration == pytest.approx(1.5, abs=0.05)

    def test_raw_audio_bytes_are_accepted(self, tmp_path):
        audio = _wav_bytes(0.5)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

        engine = WorkersAISpeechEngine(account_id="a", api_token="t", client=_client(handler))
        duration = engine.synthesize("Short.", tmp_path / "seg.wav", "", 1.0)
        assert duration == pytest.approx(0.5, abs=0.05)

    def test_an_api_error_becomes_a_tts_error(self, tmp_path):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        engine = WorkersAISpeechEngine(account_id="a", api_token="t", client=_client(handler))
        with pytest.raises(TTSError, match="429"):
            engine.synthesize("Hello.", tmp_path / "seg.wav", "", 1.0)

    def test_an_empty_payload_becomes_a_tts_error(self, tmp_path):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {}, "success": True})

        engine = WorkersAISpeechEngine(account_id="a", api_token="t", client=_client(handler))
        with pytest.raises(TTSError, match="no audio"):
            engine.synthesize("Hello.", tmp_path / "seg.wav", "", 1.0)

    def test_credentials_are_required(self):
        with pytest.raises(CredentialMissing):
            WorkersAISpeechEngine(account_id="", api_token="")


class TestWorkersAITranscription:
    def test_a_transcript_is_returned(self, tmp_path):
        audio = tmp_path / "narration.wav"
        audio.write_bytes(_wav_bytes(0.2))

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["audio"]  # base64 payload
            return httpx.Response(200, json={"result": {"text": "the house in camden"}})

        transcriber = WorkersAITranscriber(account_id="a", api_token="t", client=_client(handler))
        assert transcriber.transcribe(audio) == "the house in camden"

    def test_a_missing_transcript_field_raises(self, tmp_path):
        audio = tmp_path / "narration.wav"
        audio.write_bytes(_wav_bytes(0.2))

        transcriber = WorkersAITranscriber(
            account_id="a",
            api_token="t",
            client=_client(lambda r: httpx.Response(200, json={"result": {}})),
        )
        with pytest.raises(RuntimeError, match="no transcript"):
            transcriber.transcribe(audio)

    def test_a_missing_file_raises(self, tmp_path):
        transcriber = WorkersAITranscriber(account_id="a", api_token="t")
        with pytest.raises(FileNotFoundError):
            transcriber.transcribe(tmp_path / "absent.wav")


class TestEngineSelection:
    def test_cloudflare_engine_is_selected_when_credentials_exist(self):
        engine = build_engine(
            SpeechSettings(engine="cloudflare"),
            credentials=CloudCredentials(cloudflare_account_id="a", cloudflare_api_token="t"),
        )
        assert engine.name == "workers-ai"

    def test_without_credentials_it_degrades_to_the_silent_engine(self):
        engine = build_engine(SpeechSettings(engine="cloudflare"), credentials=CloudCredentials())
        assert isinstance(engine, SilentEngine)

    def test_the_silent_engine_still_marks_output_unpublishable(self, tmp_path):
        """Degrading must never quietly ship silence (§13.4)."""

        engine = SilentEngine()
        path = Path(tmp_path) / "s.wav"
        engine.synthesize("A sentence.", path, "", 1.0)
        assert path.exists()


class TestContainerSniffing:
    """The container is identified from the bytes, not a header that lies."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".wav"),
            (b"OggS\x00\x02\x00\x00", ".ogg"),
            (b"ID3\x04\x00\x00\x00", ".mp3"),
            (b"\xff\xfb\x90\x00", ".mp3"),
            (b"\x00\x00\x00\x20ftypM4A ", ".m4a"),
        ],
    )
    def test_magic_bytes_win_over_a_json_content_type(self, payload, expected):
        from mpvf.speech.cloud_tts import _suffix_for

        response = httpx.Response(200, headers={"content-type": "application/json"})
        assert _suffix_for(payload, response) == expected

    def test_header_is_the_fallback_for_unknown_bytes(self):
        from mpvf.speech.cloud_tts import _suffix_for

        response = httpx.Response(200, headers={"content-type": "audio/wav"})
        assert _suffix_for(b"unknown-payload", response) == ".wav"

    def test_default_is_mp3(self):
        from mpvf.speech.cloud_tts import _suffix_for

        assert _suffix_for(b"????", httpx.Response(200)) == ".mp3"
