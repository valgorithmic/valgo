from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    IntegrityError,
    NotFoundError,
    TransferError,
    ValgoError,
    ValidationError,
)
from .models import Artifact, BatchUploadResult, DeletionResult, UploadFailure, UploadResult


class Valgo:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30,
        max_workers: int = 4,
    ) -> None:
        self.api_key = api_key or os.getenv("VALGO_API_KEY")
        if not self.api_key:
            raise ValueError("api_key is required (or set VALGO_API_KEY)")
        self.base_url = (base_url or os.getenv("VALGO_BASE_URL") or "https://api.valgo.ai").rstrip("/")
        self.max_workers = max(1, max_workers)
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "valgo-python/0.2.0"},
        )
        # Presigned object-store requests must never inherit the Valgo bearer credential.
        self._storage_http = httpx.Client(timeout=timeout, headers={"User-Agent": "valgo-python/0.2.0"})

    def __enter__(self) -> Valgo:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()
        self._storage_http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        response: httpx.Response | None = None
        for attempt in range(5):
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise TransferError(f"Valgo API request failed: {exc}") from exc
                self._backoff(attempt)
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 4:
                break
            self._backoff(attempt)
        assert response is not None
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            errors: dict[int, type[ValgoError]] = {
                401: AuthenticationError,
                403: AuthorizationError,
                404: NotFoundError,
                409: ConflictError,
                422: ValidationError,
            }
            error = errors.get(response.status_code, TransferError)
            raise error(f"Valgo API returned {response.status_code}: {detail}")
        return response.json() if response.content else {}

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(0.25 * (2**attempt) + random.random() * 0.1, 3.0))

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def upload(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UploadResult | BatchUploadResult:
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(source)
        if source.is_symlink():
            raise ValidationError("symbolic links are not uploaded")
        if source.is_file():
            return self._upload_file(source, name or source.name, metadata or {})
        if not source.is_dir():
            raise ValidationError("upload source must be a regular file or directory")
        files = sorted(item for item in source.rglob("*") if item.is_file() and not item.is_symlink())
        completed: list[UploadResult] = []
        failures: list[UploadFailure] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            for item in files:
                relative = item.relative_to(source).as_posix()
                logical_name = f"{name.strip('/')}/{relative}" if name else relative
                futures[pool.submit(self._upload_file, item, logical_name, metadata or {})] = item
            for future in as_completed(futures):
                item = futures[future]
                try:
                    completed.append(future.result())
                except Exception as exc:  # batch contract captures per-file failures
                    failures.append(UploadFailure(path=item, error=exc))
        completed.sort(key=lambda item: str(item.path))
        failures.sort(key=lambda item: str(item.path))
        return BatchUploadResult(completed=completed, failures=failures)

    def _upload_file(self, path: Path, name: str, metadata: dict[str, Any]) -> UploadResult:
        size = path.stat().st_size
        checksum = self._hash_file(path)
        idempotency = hashlib.sha256(f"{name}\0{size}\0{checksum}".encode()).hexdigest()
        initiation = self._request(
            "POST",
            "/v1/uploads",
            json={
                "name": name,
                "size_bytes": size,
                "checksum_sha256": checksum,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "metadata": metadata,
                "idempotency_key": idempotency,
            },
        )
        resumed = bool(initiation.get("uploaded_parts"))
        if initiation["status"] == "completed":
            artifact = Artifact(
                artifact_id=initiation["artifact_id"],
                artifact_version_id=initiation["artifact_version_id"],
                name=initiation["name"],
                version=initiation["version"],
                size_bytes=size,
                checksum_sha256=checksum,
                storage_mode="unknown",
            )
            return UploadResult(path, artifact, initiation["transfer_id"], resumed=True)
        if initiation["strategy"] == "single":
            self._upload_single(path, initiation)
            parts: list[dict[str, Any]] = []
        else:
            parts = self._upload_multipart(path, initiation)
        artifact_data = self._request(
            "POST", f"/v1/uploads/{initiation['transfer_id']}/complete", json={"parts": parts}
        )
        return UploadResult(
            path=path,
            artifact=Artifact.from_dict(artifact_data),
            transfer_id=initiation["transfer_id"],
            resumed=resumed,
        )

    def _upload_single(self, path: Path, initiation: dict[str, Any]) -> None:
        current = initiation
        last_error = "no storage response"
        for attempt in range(5):
            headers = dict(current.get("required_headers", {}))
            headers["content-length"] = str(path.stat().st_size)
            with path.open("rb") as stream:
                response = self._storage_http.put(current["upload_url"], headers=headers, content=stream)
            if response.is_success:
                return
            detail = response.text.strip().replace("\n", " ")[:500]
            last_error = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
            if response.status_code == 403:
                current = self._request("GET", f"/v1/uploads/{initiation['transfer_id']}")
            elif response.status_code >= 500:
                self._backoff(attempt)
            else:
                raise TransferError(f"object upload failed with status {response.status_code}")
        raise TransferError(f"object upload failed after retries ({last_error})")

    def _upload_multipart(self, path: Path, initiation: dict[str, Any]) -> list[dict[str, Any]]:
        part_size = int(initiation["part_size"])
        size = path.stat().st_size
        total = (size + part_size - 1) // part_size
        completed = {part["part_number"]: part for part in initiation.get("uploaded_parts", [])}
        missing = [number for number in range(1, total + 1) if number not in completed]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._upload_part, path, initiation["transfer_id"], number, part_size): number
                for number in missing
            }
            for future in as_completed(futures):
                part = future.result()
                completed[part["part_number"]] = part
        return [completed[number] for number in sorted(completed)]

    def _upload_part(self, path: Path, transfer_id: str, number: int, part_size: int) -> dict[str, Any]:
        with path.open("rb") as stream:
            stream.seek((number - 1) * part_size)
            data = stream.read(part_size)
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        for attempt in range(5):
            signed = self._request(
                "POST",
                f"/v1/uploads/{transfer_id}/parts/presign",
                json={"parts": [{"part_number": number, "checksum_sha256": checksum}]},
            )["parts"][0]
            response = self._storage_http.put(
                signed["upload_url"],
                headers=signed["required_headers"] | {"content-length": str(len(data))},
                content=data,
            )
            if response.is_success:
                return {
                    "part_number": number,
                    "etag": response.headers["etag"],
                    "checksum_sha256": response.headers.get("x-amz-checksum-sha256", checksum),
                    "size_bytes": len(data),
                }
            if response.status_code not in {403, 429} and response.status_code < 500:
                raise TransferError(f"part {number} failed with status {response.status_code}")
            self._backoff(attempt)
        raise TransferError(f"part {number} failed after retries")

    def attach(
        self,
        s3_uri: str,
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        checksum_sha256: str | None = None,
    ) -> Artifact:
        value = self._request(
            "POST",
            "/v1/attachments",
            json={
                "uri": s3_uri,
                "name": name,
                "metadata": metadata or {},
                "checksum_sha256": checksum_sha256,
            },
        )
        return Artifact.from_dict(value)

    def download(
        self,
        artifact_id_or_name: str,
        destination: str | os.PathLike[str] | None = None,
        *,
        overwrite: bool = False,
    ) -> Path:
        signed = self._request("POST", "/v1/downloads", json={"artifact": artifact_id_or_name})
        artifact = Artifact.from_dict(signed["artifact"])
        target = Path(destination) if destination is not None else Path(artifact.name).name
        target = Path(target)
        if target.exists() and not overwrite:
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.valgo-part")
        digest = hashlib.sha256()
        size = 0
        try:
            with self._storage_http.stream("GET", signed["download_url"]) as response:
                if response.is_error:
                    raise TransferError(f"object download failed with status {response.status_code}")
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            if size != artifact.size_bytes:
                raise IntegrityError("downloaded size does not match artifact metadata")
            if artifact.checksum_sha256 and digest.hexdigest() != artifact.checksum_sha256:
                raise IntegrityError("downloaded checksum does not match artifact metadata")
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target

    def delete(
        self,
        artifact_id_or_name: str,
        *,
        all_versions: bool = False,
        delete_source: bool = False,
    ) -> DeletionResult:
        """Delete an exact version, or explicitly delete every version of a logical artifact."""
        value = self._request(
            "POST",
            "/v1/deletions",
            json={
                "artifact": artifact_id_or_name,
                "all_versions": all_versions,
                "delete_source": delete_source,
            },
        )
        return DeletionResult.from_dict(value)
