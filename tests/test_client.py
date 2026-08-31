from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from valgo import BatchUploadResult, IntegrityError, Valgo


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
