"""YouTube publishing (§7.17).

The Data API client sits behind an interface so the pipeline can be exercised
without credentials. Uploads are resumable, private by default, and idempotent:
the same render is never uploaded twice unless an editor explicitly creates a
replacement publication (FR-179).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mpvf.models.domain import PublicationMetadata, PublicationResult, utcnow
from mpvf.observability.logging import get_logger

logger = get_logger("publish.youtube")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# YouTube restricts uploads from unaudited API projects to private (§7.17).
UNVERIFIED_PROJECT_HINT = (
    "This API project has not completed YouTube's audit, so uploads stay private "
    "and cannot be made public from the API. Request an audit in the Google Cloud "
    "console; until then use private-upload mode and publish manually."
)


class PublishError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CredentialsMissing(PublishError):
    def __init__(self, detail: str) -> None:
        super().__init__("credentials_missing", detail, retryable=False)


@dataclass
class UploadRequest:
    video_path: Path
    metadata: PublicationMetadata
    thumbnail_path: Path | None = None
    captions_path: Path | None = None
    playlist_id: str | None = None
    idempotency_key: str = ""


@runtime_checkable
class Publisher(Protocol):
    name: str

    def available(self) -> bool: ...

    def upload(self, request: UploadRequest) -> PublicationResult: ...

    def processing_state(self, video_id: str) -> str: ...


def idempotency_key(run_id: str, video_path: Path | str, metadata: PublicationMetadata) -> str:
    """Stable key over the render bytes and the metadata that matters."""

    digest = hashlib.sha256()
    digest.update(run_id.encode("utf-8"))
    path = Path(video_path)
    if path.exists():
        stat = path.stat()
        digest.update(f"{path.name}:{stat.st_size}".encode())
        with path.open("rb") as handle:
            digest.update(handle.read(1024 * 1024))
    digest.update(metadata.title.encode("utf-8"))
    digest.update(metadata.privacy.encode("utf-8"))
    return digest.hexdigest()[:32]


class DryRunPublisher:
    """Writes the upload request to disk instead of calling YouTube."""

    name = "dry-run"

    def __init__(self, destination: Path | str) -> None:
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)

    def available(self) -> bool:
        return True

    def upload(self, request: UploadRequest) -> PublicationResult:
        payload = {
            "video_path": str(request.video_path),
            "thumbnail_path": str(request.thumbnail_path) if request.thumbnail_path else None,
            "captions_path": str(request.captions_path) if request.captions_path else None,
            "metadata": request.metadata.model_dump(mode="json"),
        }
        target = self.destination / "upload-request.json"
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return PublicationResult(
            run_id="",
            video_id=None,
            url=None,
            privacy=request.metadata.privacy,
            upload_state="pending",
            processing_state="not_uploaded",
            api_response={"dry_run": True, "written_to": str(target)},
            idempotency_key=request.idempotency_key,
        )

    def processing_state(self, video_id: str) -> str:
        return "not_uploaded"


class YouTubePublisher:
    """Resumable upload through the YouTube Data API v3 with OAuth 2.0."""

    name = "youtube"

    def __init__(
        self,
        client_secrets_path: Path | str,
        token_path: Path | str,
        chunk_size: int = 4 * 1024 * 1024,
        max_attempts: int = 5,
    ) -> None:
        self.client_secrets_path = Path(client_secrets_path)
        self.token_path = Path(token_path)
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self._service: Any = None

    # -- credentials ------------------------------------------------------
    def available(self) -> bool:
        try:
            import google_auth_oauthlib.flow  # noqa: F401
            import googleapiclient.discovery  # noqa: F401
        except ImportError:
            return False
        return self.token_path.exists() or self.client_secrets_path.exists()

    def credential_state(self) -> dict[str, Any]:
        return {
            "client_secrets_present": self.client_secrets_path.exists(),
            "token_present": self.token_path.exists(),
            "libraries_installed": self._libraries_installed(),
        }

    @staticmethod
    def _libraries_installed() -> bool:
        try:
            import google_auth_oauthlib.flow  # noqa: F401
            import googleapiclient.discovery  # noqa: F401
        except ImportError:
            return False
        return True

    def authorize(self, headless: bool = False) -> Path:  # pragma: no cover - interactive
        """Run the OAuth installed-app flow and persist the refresh token."""

        if not self._libraries_installed():
            raise CredentialsMissing(
                "google-api-python-client and google-auth-oauthlib are required; "
                "install the 'publish' extra"
            )
        if not self.client_secrets_path.exists():
            raise CredentialsMissing(f"client secrets not found at {self.client_secrets_path}")

        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets_path), SCOPES)
        credentials = flow.run_console() if headless else flow.run_local_server(port=0)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        self.token_path.chmod(0o600)
        return self.token_path

    def _credentials(self) -> Any:  # pragma: no cover - needs credentials
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.token_path.exists():
            raise CredentialsMissing(
                f"no stored token at {self.token_path}; run 'mpvf youtube auth' first"
            )
        credentials = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise CredentialsMissing("stored YouTube credentials are invalid; reauthorize")
        return credentials

    def _client(self) -> Any:  # pragma: no cover - needs credentials
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build("youtube", "v3", credentials=self._credentials())
        return self._service

    # -- upload -----------------------------------------------------------
    def upload(self, request: UploadRequest) -> PublicationResult:  # pragma: no cover - network
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        if not request.video_path.exists():
            raise PublishError("missing_master", f"no file at {request.video_path}")

        metadata = request.metadata
        body: dict[str, Any] = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "categoryId": metadata.category_id,
                "defaultLanguage": metadata.language,
                "defaultAudioLanguage": metadata.language,
            },
            "status": {
                "privacyStatus": metadata.privacy,
                "selfDeclaredMadeForKids": metadata.made_for_kids,
                "embeddable": metadata.embeddable,
            },
        }
        if metadata.scheduled_publish_at:
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = metadata.scheduled_publish_at.isoformat()

        media = MediaFileUpload(
            str(request.video_path), chunksize=self.chunk_size, resumable=True, mimetype="video/mp4"
        )
        insert = self._client().videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        attempt = 0
        while response is None:
            try:
                _status, response = insert.next_chunk()
            except HttpError as exc:
                attempt += 1
                if attempt >= self.max_attempts or exc.resp.status not in {500, 502, 503, 504}:
                    raise PublishError(
                        "upload_failed",
                        _explain_http_error(exc),
                        retryable=exc.resp.status in {500, 502, 503, 504},
                    ) from exc
                sleep_for = min(2**attempt, 60)
                logger.warning(
                    "resumable upload retry",
                    extra={"detail": {"attempt": attempt, "sleep": sleep_for}},
                )
                time.sleep(sleep_for)

        video_id = response.get("id")
        result = PublicationResult(
            run_id="",
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            privacy=response.get("status", {}).get("privacyStatus", metadata.privacy),
            upload_state="scheduled" if metadata.scheduled_publish_at else "uploaded",
            uploaded_at=utcnow(),
            scheduled_for=metadata.scheduled_publish_at,
            api_response=response,
            idempotency_key=request.idempotency_key,
        )

        if video_id and request.thumbnail_path and Path(request.thumbnail_path).exists():
            self._set_thumbnail(video_id, request.thumbnail_path)
        if video_id and request.captions_path and Path(request.captions_path).exists():
            self._upload_captions(video_id, request.captions_path)
        if video_id and request.playlist_id:
            self._add_to_playlist(video_id, request.playlist_id)
        return result

    def _set_thumbnail(self, video_id: str, path: Path) -> None:  # pragma: no cover - network
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        try:
            self._client().thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(str(path))
            ).execute()
        except HttpError as exc:
            logger.warning("thumbnail set failed", extra={"detail": {"error": str(exc)}})

    def _upload_captions(self, video_id: str, path: Path) -> None:  # pragma: no cover - network
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        try:
            self._client().captions().insert(
                part="snippet",
                body={"snippet": {"videoId": video_id, "language": "en", "name": "English"}},
                media_body=MediaFileUpload(str(path)),
            ).execute()
        except HttpError as exc:
            logger.warning("caption upload failed", extra={"detail": {"error": str(exc)}})

    def _add_to_playlist(self, video_id: str, playlist_id: str) -> None:  # pragma: no cover
        from googleapiclient.errors import HttpError

        try:
            self._client().playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
        except HttpError as exc:
            logger.warning("playlist insert failed", extra={"detail": {"error": str(exc)}})

    def resolve_playlist(self, title: str) -> str | None:  # pragma: no cover - network
        if not title:
            return None
        response = (
            self._client().playlists().list(part="snippet", mine=True, maxResults=50).execute()
        )
        for item in response.get("items", []):
            if item.get("snippet", {}).get("title", "").strip().lower() == title.strip().lower():
                return item["id"]
        return None

    def processing_state(self, video_id: str) -> str:  # pragma: no cover - network
        response = (
            self._client().videos().list(part="processingDetails,status", id=video_id).execute()
        )
        items = response.get("items", [])
        if not items:
            return "unknown"
        return items[0].get("processingDetails", {}).get("processingStatus", "unknown")


def _explain_http_error(exc: Any) -> str:  # pragma: no cover - needs API error
    """Turn an API error into something an operator can act on (FR-176)."""

    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = str(exc)
    if status == 403 and "youtubeSignupRequired" in detail:
        return "the authorized Google account has no YouTube channel"
    if status == 403 and ("quotaExceeded" in detail or "uploadLimitExceeded" in detail):
        return "YouTube API quota or daily upload limit reached; retry tomorrow"
    if "youtubeAudit" in detail or "unverified" in detail.lower():
        return UNVERIFIED_PROJECT_HINT
    if status == 401:
        return "OAuth credentials expired or revoked; run 'mpvf youtube auth' again"
    return f"YouTube API error {status}: {detail[:400]}"


def build_publisher(
    secrets_dir: Path | str,
    dry_run_dir: Path | str,
    enabled: bool = True,
) -> Publisher:
    secrets_dir = Path(secrets_dir)
    publisher = YouTubePublisher(
        client_secrets_path=secrets_dir / "youtube-client-secret.json",
        token_path=secrets_dir / "youtube-token.json",
    )
    if enabled and publisher.available():
        return publisher
    return DryRunPublisher(dry_run_dir)
