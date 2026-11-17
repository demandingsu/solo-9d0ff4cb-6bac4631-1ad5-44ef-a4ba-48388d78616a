"""Domain types, validation and content fingerprints."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

NUMBER_RE = re.compile(r"^[\w.-]{1,100}$", re.UNICODE)
MIME_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:\s*;\s*charset=[A-Za-z0-9._-]+)?$"
)


class ArchiveError(Exception):
    """Base class for expected, user-facing archive failures."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidNumber(ArchiveError):
    pass


class SourceError(ArchiveError):
    pass


class SourceNotFound(SourceError):
    pass


class SourceWithdrawn(SourceError):
    pass


class SourceUnavailable(SourceError):
    pass


class SourceInconsistent(SourceError):
    """The source itself is internally inconsistent or changed mid-capture."""


class RevisionMismatch(SourceInconsistent):
    pass


class ArchiveStorageError(ArchiveError):
    pass


class InvalidCutoff(ArchiveError):
    pass


class NothingToDestroy(ArchiveError):
    pass


class NotYetDueForDestruction(ArchiveError):
    pass


class NoticeDestroyed(ArchiveError):
    pass


def normalize_number(value: str) -> str:
    number = (value or "").strip()
    if not number or not NUMBER_RE.fullmatch(number):
        raise InvalidNumber(
            "公告号无效：请使用字母、数字、汉字、下划线、点或连字符，长度 1–100。"
        )
    return number


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_utc_cutoff(value: str) -> datetime:
    """Parse a UTC cutoff instant submitted by the archivist.

    Only timezone-aware instants are accepted; a naive value is rejected so the
    operator cannot accidentally pass local time as UTC. The returned value is
    always a timezone-aware UTC datetime.
    """
    text = (value or "").strip()
    if not text:
        raise InvalidCutoff("截止时刻无效：请填写 UTC 时间，例如 2026-12-31T23:59:59Z。")
    if len(text) > 100:
        raise InvalidCutoff("截止时刻无效：输入过长。")
    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InvalidCutoff(
            "截止时刻无效：请使用 ISO 8601 的 UTC 时间，"
            "例如 2026-12-31T23:59:59Z。"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidCutoff("截止时刻无效：必须带 UTC 时区（以 Z 或 +00:00 结尾）。")
    return parsed.astimezone(timezone.utc)


def cutoff_to_iso(cutoff: datetime) -> str:
    """Render a UTC cutoff datetime in the same Z-suffixed ISO form we store."""
    return (
        cutoff.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_iso_instant(value: str) -> datetime:
    """Parse a stored Z-suffixed ISO instant into a timezone-aware datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_content_hash(
    *,
    title: str,
    body: str,
    attachment_filename: str,
    attachment_content_type: str,
    attachment_sha256: str,
) -> str:
    """Fingerprint everything that makes a site revision's content.

    Retraction state is deliberately excluded: retracting or restoring a notice
    changes source status, not the archived text/attachment pair.
    """
    payload = {
        "title": title,
        "body": body,
        "attachment": {
            "filename": attachment_filename,
            "content_type": attachment_content_type,
            "sha256": attachment_sha256,
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(encoded)


def sanitize_filename(value: str, fallback: str = "attachment.bin") -> str:
    name = (value or "").replace("\\", "/").split("/")[-1]
    name = "".join(
        ch for ch in name if ch.isprintable() and ch not in "\r\n\t"
    ).strip().strip(".")
    if not name or name in {".", ".."}:
        return fallback
    if len(name) > 180:
        suffix = ""
        if "." in name:
            suffix = "." + name.rsplit(".", 1)[-1][:20]
        name = name[: 180 - len(suffix)] + suffix
    return name


def sanitize_content_type(value: str | None) -> str:
    candidate = (value or "").split("\r")[0].split("\n")[0].strip()
    if candidate and MIME_RE.fullmatch(candidate):
        return candidate
    return "application/octet-stream"


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    text = value
    if text.endswith("Z"):
        text = text[:-1]
    text = text.replace("T", " ")
    return text + " UTC" if value.endswith("Z") else text


@dataclass(frozen=True)
class AttachmentSnapshot:
    filename: str
    content_type: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class SourceSnapshot:
    number: str
    title: str
    body: str
    body_sha256: str
    revision: int
    body_revision: int
    attachment_revision: int
    retracted: bool
    source_url: str
    attachment: AttachmentSnapshot
    source_updated_at: str | None = None

    @property
    def content_hash(self) -> str:
        return canonical_content_hash(
            title=self.title,
            body=self.body,
            attachment_filename=self.attachment.filename,
            attachment_content_type=self.attachment.content_type,
            attachment_sha256=self.attachment.sha256,
        )


@dataclass(frozen=True)
class VersionRecord:
    announcement_number: str
    revision: int
    body_revision: int
    attachment_revision: int
    title: str
    body: str
    body_sha256: str
    content_hash: str
    attachment_filename: str
    attachment_content_type: str
    attachment_size: int
    attachment_sha256: str
    attachment_blob: bytes
    source_retracted: bool
    source_url: str
    source_updated_at: str | None
    captured_at: str

    @classmethod
    def from_snapshot(
        cls, snapshot: SourceSnapshot, captured_at: str
    ) -> "VersionRecord":
        attachment = snapshot.attachment
        return cls(
            announcement_number=snapshot.number,
            revision=snapshot.revision,
            body_revision=snapshot.body_revision,
            attachment_revision=snapshot.attachment_revision,
            title=snapshot.title,
            body=snapshot.body,
            body_sha256=snapshot.body_sha256,
            content_hash=snapshot.content_hash,
            attachment_filename=attachment.filename,
            attachment_content_type=attachment.content_type,
            attachment_size=attachment.size,
            attachment_sha256=attachment.sha256,
            attachment_blob=attachment.data,
            source_retracted=snapshot.retracted,
            source_url=snapshot.source_url,
            source_updated_at=snapshot.source_updated_at,
            captured_at=captured_at,
        )


CaptureStatus = Literal["created", "unchanged", "conflict", "retracted_update"]


DestructionStatus = Literal["destroyed", "already_destroyed", "rejected"]


@dataclass(frozen=True)
class VersionFingerprint:
    """Verifiable evidence of one destroyed revision, holding no originals."""

    revision: int
    body_revision: int
    attachment_revision: int
    body_sha256: str
    attachment_sha256: str
    content_hash: str
    captured_at: str


@dataclass(frozen=True)
class DestructionCertificate:
    """The destruction voucher: fingerprints and timing, never original text."""

    announcement_number: str
    cutoff_at: str
    destroyed_at: str
    version_count: int
    fingerprints: tuple[VersionFingerprint, ...]


@dataclass(frozen=True)
class DestructionResult:
    status: DestructionStatus
    certificate: DestructionCertificate
    message: str


@dataclass(frozen=True)
class CaptureResult:
    status: CaptureStatus
    number: str
    revision: int
    message: str
    details: dict[str, Any] = field(default_factory=dict)
