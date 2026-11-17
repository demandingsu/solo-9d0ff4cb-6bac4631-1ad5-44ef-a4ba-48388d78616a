"""Web UI behaviour for retention destruction and post-destruction access."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

NUMBER = "ZFCG-2026-0001"
FAR_FUTURE = "2099-01-01T00:00:00Z"


def _archive_app(tmp_path: Path, demo):
    db = tmp_path / "archive-web-destroy.db"
    app = create_app(
        db_path=str(db),
        source_base_url="http://demo.test",
        source_client=demo,
    )
    return app


def _capture(client: TestClient) -> None:
    response = client.post("/capture", data={"number": NUMBER},
                           follow_redirects=False)
    assert response.status_code == 303


def test_destroy_form_full_flow_voucher_page_and_gone_routes(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        _capture(client)
        detail = client.get(f"/notice/{NUMBER}")
        assert "办理销毁" in detail.text
        body_text = "市政道路照明设备采购"

        download = client.get(f"/notice/{NUMBER}/version/1/attachment")
        assert download.status_code == 200
        original_bytes = download.content
        assert original_bytes

        # Submit destruction.
        response = client.post(
            "/destroy",
            data={"number": NUMBER, "cutoff": FAR_FUTURE},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert f"/notice/{NUMBER}?destroy=destroyed" in response.headers["location"]

        # The notice page now renders the certificate, not the originals.
        page = client.get(f"/notice/{NUMBER}")
        assert page.status_code == 200
        assert "销毁凭证" in page.text
        assert "各修订销毁指纹" in page.text
        assert body_text not in page.text  # original body text must not leak
        # Fingerprints are shown.
        cert = app.state.store.get_certificate(NUMBER)
        assert cert is not None
        assert cert.fingerprints[0].content_hash in page.text

        # Old version page and download entry no longer serve originals.
        version_page = client.get(f"/notice/{NUMBER}/version/1")
        assert version_page.status_code == 410
        assert "已销毁" in version_page.text
        assert body_text not in version_page.text

        gone_download = client.get(f"/notice/{NUMBER}/version/1/attachment")
        assert gone_download.status_code == 410
        assert gone_download.content != original_bytes

        # The index marks the announcement as destroyed.
        index = client.get("/")
        assert "已销毁" in index.text


def test_not_due_destruction_rejected_via_form_keeps_download(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        _capture(client)
        original = client.get(
            f"/notice/{NUMBER}/version/1/attachment"
        ).content

        response = client.post(
            "/destroy",
            data={"number": NUMBER, "cutoff": "2000-01-01T00:00:00Z"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert "error=" in location

        page = client.get(location)
        assert "不早于截止时刻" in page.text or "销毁被拒" in page.text

        # Archive intact, no voucher, download still works.
        assert app.state.store.get_certificate(NUMBER) is None
        still = client.get(f"/notice/{NUMBER}/version/1/attachment")
        assert still.status_code == 200
        assert still.content == original
        notice = client.get(f"/notice/{NUMBER}")
        assert "办理销毁" in notice.text  # form still available


def test_invalid_cutoff_returns_error_without_touching_archive(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        _capture(client)
        response = client.post(
            "/destroy",
            data={"number": NUMBER, "cutoff": "not-a-utc-timestamp"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert "截止时刻无效" in page.text
        assert app.state.store.get_certificate(NUMBER) is None
        assert len(app.state.store.list_versions(NUMBER)) == 1


def test_repeat_destroy_returns_original_certificate(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        _capture(client)
        first = client.post(
            "/destroy", data={"number": NUMBER, "cutoff": FAR_FUTURE},
            follow_redirects=False,
        )
        assert "destroy=destroyed" in first.headers["location"]

        second = client.post(
            "/destroy",
            data={"number": NUMBER, "cutoff": "2100-06-01T00:00:00Z"},
            follow_redirects=False,
        )
        assert second.status_code == 303
        assert "destroy=already_destroyed" in second.headers["location"]

        page = client.get(second.headers["location"])
        assert "原销毁凭证" in page.text
        assert app.state.store.get_certificate(NUMBER).cutoff_at == FAR_FUTURE


def test_recapture_after_destruction_is_refused_via_web(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        _capture(client)
        client.post(
            "/destroy", data={"number": NUMBER, "cutoff": FAR_FUTURE},
            follow_redirects=False,
        )

        # Same-number recapture through the existing capture form.
        response = client.post(
            "/capture", data={"number": NUMBER}, follow_redirects=False
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert "已办理销毁" in page.text
        assert "同号重采被拒绝" in page.text

        # No versions reappeared; certificate page still what the visitor sees.
        assert app.state.store.list_versions(NUMBER) == []
        assert client.get(f"/notice/{NUMBER}").status_code == 200
        assert "销毁凭证" in client.get(f"/notice/{NUMBER}").text


def test_destroy_unknown_number_without_versions_errors(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        response = client.post(
            "/destroy",
            data={"number": "NO-SUCH-7777", "cutoff": FAR_FUTURE},
            follow_redirects=False,
        )
        page = client.get(response.headers["location"])
        assert "没有可销毁的存档版本" in page.text
        assert app.state.store.get_certificate("NO-SUCH-7777") is None


def test_unrelated_announcement_keeps_its_archive_after_other_destroyed(
    tmp_path, demo
):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        client.post("/capture", data={"number": NUMBER})
        client.post("/capture", data={"number": "ZFCG-2026-0002"})
        client.post(
            "/destroy", data={"number": NUMBER, "cutoff": FAR_FUTURE},
            follow_redirects=False,
        )

        other = client.get("/notice/ZFCG-2026-0002")
        assert other.status_code == 200
        assert "销毁凭证" not in other.text
        download = client.get("/notice/ZFCG-2026-0002/version/1/attachment")
        assert download.status_code == 200
        assert download.content
