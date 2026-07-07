from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class SafetyLevel(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    ARBITRARY_EXECUTION = "arbitrary_execution"


class DeviceNameType(str, Enum):
    GUESS = "guess"
    ADDRESS = "address"
    SERVICE_NAME = "service_name"


@dataclass
class DeviceRef:
    target: str
    login: str | None = None
    http_port: int = 32000
    name_type: DeviceNameType = DeviceNameType.GUESS


@dataclass
class DeviceInfo:
    id: str
    name: str
    address: str | None = None
    login: str | None = None
    http_port: int = 32000
    service_name: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    last_seen: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class UploadProfile:
    device: DeviceRef
    gameid: str
    local_dir: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    runtime: str | None = None
    steam_play_debug: str = "disabled"
    delete_extraneous: bool = False
    skip_newer_files: bool = False
    verify_checksums: bool = False
    filter_args: list[str] = field(default_factory=list)
    restart_steam: bool = False
    use_mask_unmask: bool = False
    prevent_auto_repair: bool = False
    gdbserver: bool = False


@dataclass
class UploadPlan:
    profile: UploadProfile
    exists: bool
    file_count: int
    total_bytes: int
    warnings: list[str] = field(default_factory=list)
    destructive: bool = False
    rsync_filter_args: list[str] = field(default_factory=list)


@dataclass
class OperationRecord:
    id: str
    name: str
    safety: SafetyLevel
    status: str
    created_at: str
    updated_at: str
    device_id: str | None = None
    summary: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def redact(value: Any) -> Any:
    data = to_jsonable(value)
    sensitive = ("key", "token", "password", "secret")
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, item in data.items():
            if any(part in key.lower() for part in sensitive):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data

