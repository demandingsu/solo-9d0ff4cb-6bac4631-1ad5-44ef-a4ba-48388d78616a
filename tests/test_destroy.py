"""Retention-period destruction: eligibility, voucher, idempotency, interleave."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.capture import CaptureService
from app.destruction import DestructionService
from app.domain import (
    ArchiveStorageError,
    InvalidCutoff,
    NoticeDestroyed,
    NotYetDueForDestruction,
    NothingToDestroy,
    cutoff_to_iso,
    parse_iso_instant,
    parse_utc_cutoff,
    utc_now_iso,
)
from app.store import ArchiveStore

NUMBER = "ZFCG-2026-0001"
FAR_FUTURE = "2099-01-01T00:00:00Z"
FAR_PAST = "2000-01-01T00:00:00Z"


def _edit(demo, *, body="更新后的正文：投标截止时间调整。"):
    response = demo.post(
        f"/notice/{NUMBER}/edit",
        data={"title": "市政道路照明设备采购招标公告", "body": body},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _snapshot_before(store: ArchiveStore, number: str):
    return [
        {
            "revision": v.revision,
            "body_revision": v.body_revision,
            "attachment_revision": v.attachment_revision,
            "body": v.body,
            "body_sha256": v.body_sha256,
            "attachment_sha256": v.attachment_sha256,
            "content_hash": v.content_hash,
            "attachment_blob": v.attachment_blob,
            "captured_at": v.captured_at,
        }
        for v in store.list_versions(number)
    ]


# ------------------------------------------------------------- cutoff parsing

def test_cutoff_accepts_z_and_offsets_and_normalises_to_utc():
    assert cutoff_to_iso(parse_utc_cutoff("2026-12-31T23:59:59Z")) == (
        "2026-12-31T23:59:59Z"
    )
    # An explicit +02:00 offset is converted to the equivalent UTC instant.
    parsed = parse_utc_cutoff("2026-12-31T23:59:59+02:00")
    assert parsed.utcoffset().total_seconds() == 0
    assert cutoff_to_iso(parsed) == "2026-12-31T21:59:59Z"
    # Space separator is also accepted.
    assert cutoff_to_iso(parse_utc_cutoff("2026-12-31 23:59:59Z")) == (
        "2026-12-31T23:59:59Z"
    )


@pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "2026-12-31T23:59:59",
                                 "2026-13-40T99:99:99Z"])
def test_cutoff_rejects_naive_or_malformed_values(bad):
    with pytest.raises(InvalidCutoff):
        parse_utc_cutoff(bad)


# ------------------------------------------------------------- happy path

def test_destruction_when_all_versions_due_removes_originals_and_keeps_voucher(
    service, store, demo
):
    first = service.capture(NUMBER)
    _edit(demo)
    second = service.capture(NUMBER)
    assert second.revision == first.revision + 1

    before = _snapshot_before(store, NUMBER)
    assert len(before) == 2
    original_body = before[1]["body"]
    original_blob = before[1]["attachment_blob"]

    destruction = DestructionService(store)
    result = destruction.destroy(NUMBER, FAR_FUTURE)

    assert result.status == "destroyed"
    cert = result.certificate
    assert cert.announcement_number == NUMBER
    assert cert.version_count == 2
    assert cert.cutoff_at == FAR_FUTURE
    assert cert.destroyed_at <= utc_now_iso()

    # Every original is gone from the archive.
    assert store.list_versions(NUMBER) == []
    assert store.get_version(NUMBER, first.revision) is None
    assert store.get_version(NUMBER, second.revision) is None
    assert store.latest_version(NUMBER) is None

    # The voucher holds fingerprints, never the originals.
    assert store.is_destroyed(NUMBER)
    again = store.get_certificate(NUMBER)
    assert again is not None and again == cert
    assert [f.revision for f in cert.fingerprints] == [
        first.revision,
        second.revision,
    ]
    by_revision = {snapshot["revision"]: snapshot for snapshot in before}
    for fingerprint in cert.fingerprints:
        snapshot = by_revision[fingerprint.revision]
        assert fingerprint.body_sha256 == snapshot["body_sha256"]
        assert fingerprint.attachment_sha256 == snapshot["attachment_sha256"]
        assert fingerprint.content_hash == snapshot["content_hash"]
        assert fingerprint.captured_at == snapshot["captured_at"]
        assert fingerprint.body_revision == snapshot["body_revision"]
        assert fingerprint.attachment_revision == snapshot["attachment_revision"]

    # The serialized voucher must not contain original text or bytes.
    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute(
            "SELECT fingerprints_json FROM destruction_certificates "
            "WHERE announcement_number = ?",
            (NUMBER,),
        ).fetchone()
        db_blob = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE announcement_number = ?",
            (NUMBER,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert original_body not in row[0]
    assert original_blob.decode("utf-8") not in row[0]
    assert db_blob == 0

    # The audit trail records the destruction event.
    outcomes = {e["outcome"] for e in store.events_for(NUMBER)}
    assert "destroyed" in outcomes


def test_destruction_requires_strictly_earlier_captured_at(service, store, demo):
    from datetime import timedelta

    service.capture(NUMBER)
    record = store.latest_version(NUMBER)
    captured = parse_iso_instant(record.captured_at)
    # Equal-to-cutoff is not "earlier than": must be refused.
    with pytest.raises(NotYetDueForDestruction):
        DestructionService(store).destroy(NUMBER, record.captured_at)
    # One second later it becomes due.
    due = cutoff_to_iso(captured + timedelta(seconds=1))
    result = DestructionService(store).destroy(NUMBER, due)
    assert result.status == "destroyed"
    assert store.list_versions(NUMBER) == []


# ------------------------------------------------------------- refusal paths

def test_destruction_refused_when_a_version_is_not_due_keeps_full_archive(
    service, store, demo
):
    from datetime import timedelta

    first = service.capture(NUMBER)
    _edit(demo, body="第二版正文，采集时间晚于旧截止时刻")
    service.capture(NUMBER)
    second_record = store.get_version(NUMBER, first.revision + 1)
    before = _snapshot_before(store, NUMBER)

    # Cutoff between the two capture times: r1 due, r2 not due.
    cutoff = cutoff_to_iso(
        parse_iso_instant(second_record.captured_at) - timedelta(seconds=1)
    )
    with pytest.raises(NotYetDueForDestruction):
        DestructionService(store).destroy(NUMBER, cutoff)

    # No voucher, complete archive (all versions, bodies, blobs) preserved.
    assert store.get_certificate(NUMBER) is None
    assert not store.is_destroyed(NUMBER)
    after = _snapshot_before(store, NUMBER)
    assert after == before
    assert [v.revision for v in store.list_versions(NUMBER)] == [
        second_record.revision,
        first.revision,
    ]
    assert store.get_version(
        NUMBER, second_record.revision
    ).body.startswith("第二版正文")


def test_far_past_cutoff_refused_with_intact_archive(service, store):
    service.capture(NUMBER)
    with pytest.raises(NotYetDueForDestruction):
        DestructionService(store).destroy(NUMBER, FAR_PAST)
    assert store.get_certificate(NUMBER) is None
    assert len(store.list_versions(NUMBER)) == 1


def test_destroy_unknown_number_without_versions_is_nothing_to_destroy(store):
    with pytest.raises(NothingToDestroy):
        DestructionService(store).destroy("NO-SUCH-9999", FAR_FUTURE)
    assert store.get_certificate("NO-SUCH-9999") is None
    assert store.get_announcement("NO-SUCH-9999") is None


# ------------------------------------------------------------- idempotency

def test_repeat_destruction_returns_original_certificate(service, store):
    service.capture(NUMBER)
    destruction = DestructionService(store)
    first = destruction.destroy(NUMBER, FAR_FUTURE)
    assert first.status == "destroyed"

    # Even a different, later cutoff must return the original voucher.
    second = destruction.destroy(NUMBER, "2100-06-01T00:00:00Z")
    assert second.status == "already_destroyed"
    assert second.certificate == first.certificate
    assert second.certificate.destroyed_at == first.certificate.destroyed_at
    assert second.certificate.cutoff_at == FAR_FUTURE

    conn = sqlite3.connect(store.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM destruction_certificates "
            "WHERE announcement_number = ?",
            (NUMBER,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ------------------------------------------------------------- re-capture guard

def test_recapture_after_destruction_is_blocked(service, store, demo):
    service.capture(NUMBER)
    DestructionService(store).destroy(NUMBER, FAR_FUTURE)

    with pytest.raises(NoticeDestroyed):
        service.capture(NUMBER)

    # No version can reappear and the voucher is untouched.
    assert store.list_versions(NUMBER) == []
    cert = store.get_certificate(NUMBER)
    assert cert is not None and cert.version_count == 1
    events = store.events_for(NUMBER)
    assert any(e["outcome"] == "capture_blocked_destroyed" for e in events)


def test_store_rejects_direct_insert_for_destroyed_number(
    service, store, tmp_path
):
    service.capture(NUMBER)
    DestructionService(store).destroy(NUMBER, FAR_FUTURE)

    # Even bypassing the service, the write transaction itself refuses the row.
    from app.domain import (
        AttachmentSnapshot,
        SourceSnapshot,
        VersionRecord,
    )
    import hashlib

    blob = b"attempted-recapture"
    body = "试图重采的正文"
    snapshot = SourceSnapshot(
        number=NUMBER,
        title="重采标题",
        body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        revision=99,
        body_revision=99,
        attachment_revision=99,
        retracted=False,
        source_url="u",
        attachment=AttachmentSnapshot(
            "w.txt", "text/plain", len(blob),
            hashlib.sha256(blob).hexdigest(), blob,
        ),
    )
    with pytest.raises(NoticeDestroyed):
        store.insert_version(
            VersionRecord.from_snapshot(snapshot, utc_now_iso()),
            outcome="created",
            detail="should be blocked",
        )
    assert store.list_versions(NUMBER) == []


# ------------------------------------------------------------- interleaving

def test_interleaved_capture_of_not_due_revision_blocks_destruction(
    service, store, demo, tmp_path
):
    """Capture lands before the destruction transaction's eligibility read.

    The destroy must judge the versions present at commit time and refuse,
    leaving the complete archive in place.
    """
    service.capture(NUMBER)  # r1
    r1 = store.latest_version(NUMBER)
    before = _snapshot_before(store, NUMBER)

    # Backdate r1 so it is due under the 2002 cutoff; only the interleaved r2
    # (captured "now") can then be the reason for refusal.
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE versions SET captured_at = ? "
        "WHERE announcement_number = ? AND revision = 1",
        ("2001-01-01T00:00:00Z", NUMBER),
    )
    conn.commit()
    conn.close()
    r1 = store.get_version(NUMBER, 1)

    # A second writer (e.g. a concurrent capture worker) on the same database.
    other_store = ArchiveStore(tmp_path / "archive-interleave.db")
    other_store.db_path = store.db_path  # share the SQLite file

    real_connect = store._connect
    interleave_done = {"done": False}
    cutoff = "2002-01-01T00:00:00Z"

    class InterleavingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if sql == "BEGIN IMMEDIATE" and not interleave_done["done"]:
                # Simulate a capture that commits a not-yet-due revision while
                # the destruction request is in flight, before it opens its
                # write transaction.
                other_store.insert_version(
                    _make_version(r1, revision=2, captured_at=utc_now_iso()),
                    outcome="created",
                    detail="interleaved capture",
                )
                interleave_done["done"] = True
            return self._conn.execute(sql, *params)

    store._connect = lambda: InterleavingConnection(real_connect())  # type: ignore

    with pytest.raises(NotYetDueForDestruction):
        DestructionService(store).destroy(NUMBER, cutoff)

    store._connect = real_connect  # type: ignore
    assert interleave_done["done"] is True

    # Full archive preserved, no voucher created.
    assert store.get_certificate(NUMBER) is None
    revisions = sorted(v.revision for v in store.list_versions(NUMBER))
    assert revisions == [1, 2]
    # r1's content (body/attachment) is untouched apart from the test backdate.
    r1_after = store.get_version(NUMBER, 1)
    assert r1_after.body == before[0]["body"]
    assert r1_after.attachment_blob == before[0]["attachment_blob"]
    assert r1_after.content_hash == before[0]["content_hash"]


def test_interleaved_capture_of_due_revision_allows_destruction(
    service, store, tmp_path
):
    """If the interleaved revision is itself past the cutoff, destruction wins."""
    service.capture(NUMBER)
    r1 = store.latest_version(NUMBER)
    other_store = ArchiveStore(tmp_path / "interleave-due.db")
    other_store.db_path = store.db_path

    real_connect = store._connect
    done = {"done": False}

    class InterleavingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if sql == "BEGIN IMMEDIATE" and not done["done"]:
                # Old revision captured in 2001: strictly before a 2099 cutoff.
                other_store.insert_version(
                    _make_version(r1, revision=2, captured_at="2001-01-01T00:00:00Z"),
                    outcome="created",
                    detail="interleaved old capture",
                )
                done["done"] = True
            return self._conn.execute(sql, *params)

    store._connect = lambda: InterleavingConnection(real_connect())  # type: ignore
    result = DestructionService(store).destroy(NUMBER, FAR_FUTURE)
    store._connect = real_connect  # type: ignore

    assert result.status == "destroyed"
    assert result.certificate.version_count == 2
    assert store.list_versions(NUMBER) == []


# ------------------------------------------------------------- failure atomicity

def test_storage_failure_keeps_archive_and_creates_no_certificate(
    service, store, demo
):
    service.capture(NUMBER)
    _edit(demo)
    service.capture(NUMBER)
    before = _snapshot_before(store, NUMBER)

    real_connect = store._connect

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if "DELETE FROM versions" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *params)

    store._connect = lambda: FailingConnection(real_connect())  # type: ignore
    with pytest.raises(ArchiveStorageError):
        DestructionService(store).destroy(NUMBER, FAR_FUTURE)
    store._connect = real_connect  # type: ignore

    # Originals fully retained; no certificate, no destruction log, no marker.
    assert store.get_certificate(NUMBER) is None
    assert not store.is_destroyed(NUMBER)
    assert _snapshot_before(store, NUMBER) == before
    assert len(store.list_versions(NUMBER)) == 2
    assert all(e["outcome"] != "destroyed" for e in store.events_for(NUMBER))


def test_certificate_insert_failure_rolls_back_entire_destruction(
    service, store
):
    service.capture(NUMBER)
    before = _snapshot_before(store, NUMBER)
    real_connect = store._connect

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if "INSERT INTO destruction_certificates" in sql:
                raise sqlite3.OperationalError("disk full")
            return self._conn.execute(sql, *params)

    store._connect = lambda: FailingConnection(real_connect())  # type: ignore
    with pytest.raises(ArchiveStorageError):
        DestructionService(store).destroy(NUMBER, FAR_FUTURE)
    store._connect = real_connect  # type: ignore

    assert store.get_certificate(NUMBER) is None
    assert _snapshot_before(store, NUMBER) == before
    assert len(store.list_versions(NUMBER)) == 1


def test_destruction_survives_store_reopen(service, store):
    """A committed voucher is durable across reopening the database."""
    service.capture(NUMBER)
    expected = DestructionService(store).destroy(NUMBER, FAR_FUTURE).certificate

    reopened = ArchiveStore(store.db_path)
    assert reopened.list_versions(NUMBER) == []
    assert reopened.get_certificate(NUMBER) == expected
    with pytest.raises(NoticeDestroyed):
        CaptureService(reopened, service.source).capture(NUMBER)


# ------------------------------------------------------------- helpers

def _make_version(base, *, revision: int, captured_at: str):
    from app.domain import VersionRecord

    return VersionRecord(
        announcement_number=base.announcement_number,
        revision=revision,
        body_revision=revision,
        attachment_revision=revision,
        title=base.title,
        body=base.body,
        body_sha256=base.body_sha256,
        content_hash=base.content_hash,
        attachment_filename=base.attachment_filename,
        attachment_content_type=base.attachment_content_type,
        attachment_size=base.attachment_size,
        attachment_sha256=base.attachment_sha256,
        attachment_blob=base.attachment_blob,
        source_retracted=False,
        source_url=base.source_url,
        source_updated_at=base.source_updated_at,
        captured_at=captured_at,
    )
