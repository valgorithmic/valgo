from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    artifact_version_id: str
    name: str
    version: int
    size_bytes: int
    checksum_sha256: str | None
    storage_mode: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=value["artifact_id"],
            artifact_version_id=value["artifact_version_id"],
            name=value["name"],
            version=value["version"],
            size_bytes=value["size_bytes"],
            checksum_sha256=value.get("checksum_sha256"),
            storage_mode=value["storage_mode"],
        )


@dataclass(frozen=True)
class UploadResult:
    path: Path
    artifact: Artifact
    transfer_id: str
    resumed: bool = False

    @property
    def artifact_id(self) -> str:
        return self.artifact.artifact_version_id


@dataclass(frozen=True)
class UploadFailure:
    path: Path
    error: Exception


@dataclass(frozen=True)
class BatchUploadResult:
    completed: list[UploadResult] = field(default_factory=list)
    failures: list[UploadFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures
