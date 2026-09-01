from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from valgo import BatchUploadResult, IntegrityError, UploadResult, Valgo


def json_response(status: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


def test_single_upload_and_download(tmp_path: Path) -> None:
    source = tmp_path / "dataset.bin"
    source.write_bytes(b"test-data")
    checksum = hashlib.sha256(b"test-data").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/uploads":
            return json_response(
                200,
                {
                    "transfer_id": "transfer-1",
                    "artifact_id": "artifact-1",
                    "artifact_version_id": "version-1",
                    "name": "dataset.bin",
                    "version": 1,
                    "status": "uploading",
                    "strategy": "single",
                    "upload_url": "https://storage.test/object",
                    "required_headers": {},
                    "uploaded_parts": [],
                },
            )
        if request.url.host == "storage.test" and request.method == "PUT":
            assert "authorization" not in request.headers
            assert request.read() == b"test-data"
            return httpx.Response(200)
        if request.url.path.endswith("/complete"):
            return json_response(
                200,
                {
                    "artifact_id": "artifact-1",
                    "artifact_version_id": "version-1",
                    "name": "dataset.bin",
                    "version": 1,
                    "size_bytes": 9,
                    "checksum_sha256": checksum,
                    "storage_mode": "hosted",
                    "status": "completed",
                    "completed_at": None,
                },
            )
        if request.url.path == "/v1/downloads":
            return json_response(
                200,
                {
                    "artifact": {
                        "artifact_id": "artifact-1",
                        "artifact_version_id": "version-1",
                        "name": "dataset.bin",
                        "version": 1,
                        "size_bytes": 9,
                        "checksum_sha256": checksum,
                        "storage_mode": "hosted",
                    },
                    "download_url": "https://storage.test/object",
                    "expires_in": 900,
                },
            )
        if request.url.host == "storage.test" and request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"test-data")
        return httpx.Response(500)

    client = Valgo("valgo_live_test_secret", base_url="https://api.test")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._storage_http.close()
    client._storage_http = httpx.Client(transport=httpx.MockTransport(handler))

    uploaded = client.upload(source)
    assert uploaded.artifact_id == "version-1"

    destination = client.download("artifact-1", tmp_path / "download.bin")
    assert destination.read_bytes() == b"test-data"
    client.close()


def test_directory_upload_collects_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "datasets"
    directory.mkdir()
    (directory / "good.bin").write_bytes(b"good")
    (directory / "bad.bin").write_bytes(b"bad")
    client = Valgo("valgo_live_test_secret")

    def fake_upload(path: Path, name: str, metadata: dict[str, object]) -> object:
        if path.name == "bad.bin":
            raise RuntimeError("failed")
        return type("Result", (), {"path": path})()

    monkeypatch.setattr(client, "_upload_file", fake_upload)
    result = client.upload(directory)

    assert isinstance(result, BatchUploadResult)
    assert len(result.completed) == 1
    assert len(result.failures) == 1
    client.close()


def test_checksum_failure_removes_partial_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/downloads":
            return json_response(
                200,
                {
                    "artifact": {
                        "artifact_id": "artifact-1",
                        "artifact_version_id": "version-1",
                        "name": "dataset.bin",
                        "version": 1,
                        "size_bytes": 3,
                        "checksum_sha256": "0" * 64,
                        "storage_mode": "hosted",
                    },
                    "download_url": "https://storage.test/object",
                    "expires_in": 900,
                },
            )
        return httpx.Response(200, content=b"bad")

    client = Valgo("valgo_live_test_secret", base_url="https://api.test")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._storage_http.close()
    client._storage_http = httpx.Client(transport=httpx.MockTransport(handler))
    destination = tmp_path / "dataset.bin"

    with pytest.raises(IntegrityError):
        client.download("artifact-1", destination)

    assert not destination.exists()
    assert not (tmp_path / ".dataset.bin.valgo-part").exists()
    client.close()


def test_delete_exact_version_and_all_versions() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/deletions"
        body = json.loads(request.content)
        requests.append(body)
        return json_response(
            200,
            {
                "status": "deleted",
                "deleted_count": 2 if body["all_versions"] else 1,
                "detached_count": 0,
                "objects_deleted": 2 if body["all_versions"] else 1,
                "objects_pending": 0,
                "purge_after": None,
                "storage_note": "S3 versioning may retain historical object versions.",
            },
        )

    client = Valgo("valgo_live_test_secret", base_url="https://api.test")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    exact = client.delete("version-1")
    assert exact.status == "deleted"
    assert exact.deleted_count == 1
    all_versions = client.delete("dataset.bin", all_versions=True)
    assert all_versions.deleted_count == 2
    assert requests == [
        {"artifact": "version-1", "all_versions": False, "delete_source": False},
        {"artifact": "dataset.bin", "all_versions": True, "delete_source": False},
    ]
    client.close()


def test_list_artifacts_with_filters_and_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/artifacts"
        assert request.url.params["prefix"] == "reports/"
        assert request.url.params["all_versions"] == "true"
        assert request.url.params["limit"] == "25"
        assert request.url.params["cursor"] == "next-page"
        return json_response(
            200,
            {
                "items": [
                    {
                        "artifact_id": "artifact-1",
                        "artifact_version_id": "version-2",
                        "name": "reports/report.parquet",
                        "version": 2,
                        "size_bytes": 42,
                        "checksum_sha256": "a" * 64,
                        "storage_mode": "hosted",
                        "status": "completed",
                        "completed_at": "2026-08-31T12:00:00Z",
                    }
                ],
                "next_cursor": "another-page",
            },
        )

    client = Valgo("valgo_live_test_secret", base_url="https://api.test")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    page = client.list(prefix="reports/", all_versions=True, limit=25, cursor="next-page")
    assert page.items[0].name == "reports/report.parquet"
    assert page.items[0].completed_at is not None
    assert page.next_cursor == "another-page"
    client.close()


def test_completed_upload_resume_preserves_storage_mode(tmp_path: Path) -> None:
    source = tmp_path / "existing.bin"
    source.write_bytes(b"existing")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/uploads"
        return json_response(
            200,
            {
                "transfer_id": "transfer-1",
                "artifact_id": "artifact-1",
                "artifact_version_id": "version-1",
                "name": "existing.bin",
                "version": 1,
                "storage_mode": "hosted",
                "status": "completed",
            },
        )

    client = Valgo("valgo_live_test_secret", base_url="https://api.test")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    uploaded = client.upload(source)
    assert isinstance(uploaded, UploadResult)
    assert uploaded.resumed is True
    assert uploaded.artifact.storage_mode == "hosted"
    client.close()
