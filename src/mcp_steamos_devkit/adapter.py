from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko

from .config import DevkitLayout, find_layout
from .models import (
    DeviceInfo,
    DeviceNameType,
    DeviceRef,
    UploadPlan,
    UploadProfile,
    to_jsonable,
)
from .state import JsonStore


STEAMOS_DEVKIT_SERVICE = "_steamos-devkit._tcp.local."
DEFAULT_HTTP_PORT = 32000
REQUEST_TIMEOUT = 5
MAGIC_PHRASE = "900b919520e4cf601998a71eec318fec"
DEVKIT_TOOL_FOLDER = "devkit-game"
REMOTE_STEAM_LOGS_PATH = "/home/{login}/.local/share/Steam/logs"
SIDELOAD_GAMEIDS = {"steam", "steamdeckard", "steamvr", "steamvrdeckard"}
RESTART_SESSION = "/usr/bin/steamos-session-select gamescope"
REBOOT_NOW = "/usr/bin/steamos-polkit-helpers/steamos-reboot-now"

RUNTIME_ALIASES = {
    "proton-stable": "proton-stable",
    "steamplay": "proton-stable",
    "steam_play": "proton-stable",
    "proton-experimental": "proton-experimental",
    "experimental": "proton-experimental",
    "scout": "SteamLinuxRuntime",
    "slr1": "SteamLinuxRuntime",
    "sniper": "SteamLinuxRuntime_sniper",
    "slr3": "SteamLinuxRuntime_sniper",
    "steamrt4-arm64": "SteamLinuxRuntime_4-arm64",
    "slr4-arm64": "SteamLinuxRuntime_4-arm64",
    "steamrt4": "SteamLinuxRuntime_4",
    "slr4": "SteamLinuxRuntime_4",
    "android": "fauxdroid",
}

DEBUG_MODE = {
    "disabled": 0,
    "none": 0,
    "start": 1,
    "wait": 2,
}

ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
LEPTON_CONTEXT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:+\\-]+\.(service|socket|target|timer|path|mount)$")
ADB_LOG_HIGHLIGHT_RE = re.compile(
    r"Unity|XR|OpenXR|SteamAPI|Steamworks|NullReference|Exception|FATAL|"
    r"Display \(1\)|header mismatch|XR_ERROR|TMP|Error loading subsystem|Fully drawn",
    re.IGNORECASE,
)
APK_BADGING_PACKAGE_RE = re.compile(
    r"package:\s+name='(?P<name>[^']+)'\s+versionCode='(?P<version_code>[^']+)'"
    r"(?:\s+versionName='(?P<version_name>[^']*)')?"
)


class DevkitAdapterError(RuntimeError):
    pass


class SteamOSDevkitAdapter:
    def __init__(self, layout: DevkitLayout | None = None, store: JsonStore | None = None):
        self.layout = layout or find_layout()
        self.store = store or JsonStore(self.layout.data_dir)

    def doctor(self) -> dict[str, Any]:
        dependency_status: dict[str, Any] = {}
        for module in ("paramiko", "zeroconf", "platformdirs", "mcp"):
            try:
                __import__(module)
            except Exception as exc:  # pragma: no cover - diagnostic detail only
                dependency_status[module] = {"ok": False, "error": str(exc)}
            else:
                dependency_status[module] = {"ok": True}
        layout = self.layout.doctor()
        required = {
            "devkit_utils": bool(self.layout.devkit_utils_dir),
            "ssh": bool(layout["ssh"]),
            "rsync": bool(layout["rsync"]),
        }
        if platform.system() == "Windows":
            required["cygpath"] = bool(layout["cygpath"])
        return {
            "ok": all(required.values()),
            "required": required,
            "layout": layout,
            "dependencies": dependency_status,
            "adb": self.adb_doctor(),
        }

    def adb_doctor(self) -> dict[str, Any]:
        adb = self.layout.locate_adb()
        if not adb:
            return {
                "ok": False,
                "adb_path": None,
                "notes": [
                    "ADB was not found. Install Android SDK Platform Tools or set ADB_PATH.",
                    "Steam Frame Lepton ADB requires Developer Mode and a running Lepton session.",
                ],
            }
        try:
            result = self._run_adb(["version"], timeout=10)
        except Exception as exc:
            return {"ok": False, "adb_path": adb, "error": str(exc)}
        return {
            "ok": result["returncode"] == 0,
            "adb_path": adb,
            "version": result["stdout"].splitlines(),
            "notes": [
                "Wi-Fi Lepton flow: launch Lepton Development or an Android game, then adb connect frame:5555.",
                "USB Lepton flow: adb forward tcp:5555 tcp:5555, then adb connect localhost:5555.",
            ],
        }

    def adb_environment_conflict_doctor(self, host: str = "frame") -> dict[str, Any]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(path: str | None) -> None:
            if not path:
                return
            resolved = str(Path(path).expanduser())
            key = resolved.lower() if platform.system() == "Windows" else resolved
            if key not in seen:
                seen.add(key)
                candidates.append(resolved)

        add_candidate(os.environ.get("ADB_PATH"))
        add_candidate(self.layout.locate_adb())
        default_sdk = os.environ.get("LOCALAPPDATA")
        if default_sdk:
            add_candidate(str(Path(default_sdk) / "Android/Sdk/platform-tools/adb.exe"))
        path_hits: list[str] = []
        where_cmd = ["where.exe", "adb"] if platform.system() == "Windows" else ["which", "-a", "adb"]
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                where_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
                check=False,
            )
            path_hits = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            for line in path_hits:
                add_candidate(line)

        adb_versions: list[dict[str, Any]] = []
        version_banners: set[str] = set()
        for candidate in candidates:
            exists = Path(candidate).is_file()
            item: dict[str, Any] = {"path": candidate, "exists": exists}
            if exists:
                try:
                    proc = subprocess.run(
                        [candidate, "version"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
                        check=False,
                    )
                    item.update(
                        {
                            "returncode": proc.returncode,
                            "stdout": proc.stdout.splitlines(),
                            "stderr": proc.stderr.splitlines(),
                        }
                    )
                    if proc.stdout:
                        version_banners.add("\n".join(proc.stdout.splitlines()[:2]))
                except Exception as exc:
                    item["error"] = str(exc)
            adb_versions.append(item)

        devices = None
        adb = self.layout.locate_adb()
        if adb:
            with contextlib.suppress(Exception):
                devices_result = self._run_adb(["devices", "-l"], timeout=10)
                devices = {
                    **devices_result,
                    "parsed": _parse_adb_devices(devices_result.get("stdout", "")),
                }

        dns: dict[str, Any] = {"host": host, "ok": False}
        try:
            infos = socket.getaddrinfo(host, 5555, type=socket.SOCK_STREAM)
            dns["ok"] = True
            dns["addresses"] = sorted({item[4][0] for item in infos})
        except OSError as exc:
            dns["error"] = str(exc)

        notes: list[str] = []
        if len([item for item in adb_versions if item.get("exists")]) > 1:
            notes.append("Multiple adb executables were found; mismatched server/client versions can confuse Unity or Android Studio.")
        if len(version_banners) > 1:
            notes.append("Detected more than one adb version banner.")
        if devices:
            parsed = devices.get("parsed") or []
            tcp_5555 = [item for item in parsed if str(item.get("serial", "")).endswith(":5555")]
            unauthorized = [item for item in parsed if item.get("state") != "device"]
            if len(tcp_5555) > 1:
                notes.append("Multiple TCP ADB targets on port 5555 are connected; choose a serial explicitly.")
            if unauthorized:
                notes.append("At least one ADB target is not in device state.")
        if host and not dns.get("ok"):
            notes.append("Host lookup failed; try the Steam Frame IP address directly if mDNS is unreliable.")

        return {
            "ok": not notes,
            "host": host,
            "env_adb_path": os.environ.get("ADB_PATH"),
            "path_hits": path_hits,
            "candidates": adb_versions,
            "devices": devices,
            "dns": dns,
            "notes": notes,
        }

    def ensure_ssh_key(self) -> dict[str, Any]:
        key_path = self.layout.ssh_key_path
        pubkey_path = self.layout.ssh_pubkey_path
        self.layout.config_dir.mkdir(parents=True, exist_ok=True)
        key: paramiko.PKey
        try:
            key = paramiko.RSAKey.from_private_key_file(str(key_path))
        except FileNotFoundError:
            key = paramiko.RSAKey.generate(2048)
            key.write_private_key_file(str(key_path))
            pubkey_path.write_text(self._public_key_text(key), encoding="utf-8")
        except paramiko.SSHException:
            self._fix_key_permissions()
            key = paramiko.RSAKey.from_private_key_file(str(key_path))
        if not pubkey_path.is_file():
            pubkey_path.write_text(self._public_key_text(key), encoding="utf-8")
        self._fix_key_permissions()
        return {
            "key_path": str(key_path),
            "pubkey_path": str(pubkey_path),
            "public_key": pubkey_path.read_text(encoding="utf-8").strip(),
        }

    def discover_devices(self, timeout_seconds: float = 5, include_cached: bool = True) -> dict[str, Any]:
        devices: dict[str, Any] = {}
        notes: list[str] = []
        if include_cached:
            devices.update(self.store.load("devices", {}))
        try:
            discovered = self._discover_mdns(timeout_seconds)
            for device in discovered:
                data = to_jsonable(device)
                devices[device.id] = data
                self.store.update_mapping("devices", device.id, data)
        except ImportError:
            notes.append("zeroconf is not installed; discovery returned cached devices only")
        except OSError as exc:
            notes.append(f"mDNS discovery failed: {exc}")
        return {"devices": list(devices.values()), "notes": notes}

    def resolve_device(self, ref: DeviceRef, refresh_properties: bool = True) -> DeviceInfo:
        target = ref.target.strip()
        if not target:
            raise DevkitAdapterError("Device target is required")

        service_info: DeviceInfo | None = None
        if ref.name_type == DeviceNameType.SERVICE_NAME or (
            ref.name_type == DeviceNameType.GUESS and "." not in target
        ):
            service_info = self._find_service_by_name(target, ref.http_port)

        address = service_info.address if service_info and service_info.address else target
        name = service_info.name if service_info else target
        service_name = service_info.service_name if service_info else None
        http_port = service_info.http_port if service_info else ref.http_port
        properties: dict[str, Any] = {}
        login = ref.login or (service_info.login if service_info else None)
        notes: list[str] = []

        if refresh_properties:
            try:
                properties = self.get_properties(address, http_port)
                login = login or properties.get("login")
                if "settings" in properties and isinstance(properties["settings"], str):
                    with contextlib.suppress(json.JSONDecodeError):
                        properties["settings"] = json.loads(properties["settings"])
            except Exception as exc:
                notes.append(f"properties fetch failed: {exc}")

        if login is None:
            with contextlib.suppress(Exception):
                login = self.get_login_name(address, http_port)

        device = DeviceInfo(
            id=self._device_id(service_name or name, address, http_port),
            name=name,
            address=address,
            login=login,
            http_port=http_port,
            service_name=service_name,
            properties=properties,
            source="mdns" if service_info else "address",
            last_seen=self._now(),
            notes=notes,
        )
        self.store.update_mapping("devices", device.id, to_jsonable(device))
        return device

    def get_properties(self, address: str, http_port: int = DEFAULT_HTTP_PORT) -> dict[str, Any]:
        url = f"http://{address}:{http_port}/properties.json"
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def get_login_name(self, address: str, http_port: int = DEFAULT_HTTP_PORT) -> str:
        url = f"http://{address}:{http_port}/login-name"
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", "strict").strip()

    def register_device(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref, refresh_properties=False)
        key = self.ensure_ssh_key()["public_key"].strip()
        data = f"{key} {MAGIC_PHRASE}\n".encode("ascii")
        request = urllib.request.Request(
            f"http://{device.address}:{device.http_port}/register",
            data=data,
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise DevkitAdapterError(f"registration failed: HTTP {exc.code}: {detail}") from exc
        verified = False
        verify_error = None
        try:
            self.simple_ssh(device, "true", check_status=True)
            verified = True
        except Exception as exc:  # Pairing may still be awaiting target approval.
            verify_error = str(exc)
        return {"device": to_jsonable(device), "response": body, "ssh_verified": verified, "verify_error": verify_error}

    def sync_devkit_utils(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        utils = self.layout.devkit_utils_dir
        if utils is None:
            raise DevkitAdapterError("Could not locate devkit-utils")
        self.rsync_transfer(str(utils), device, "~/devkit-utils", upload=True)
        return {"device": to_jsonable(device), "remote_path": "~/devkit-utils"}

    def get_steamos_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        out, err, status = self.simple_ssh(
            device,
            "python3 ~/devkit-utils/steamos-get-status --json",
            silent=True,
            check_status=False,
        )
        if status != 0:
            raise DevkitAdapterError(f"steamos-get-status failed: {err or out}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise DevkitAdapterError(f"Could not parse steamos-get-status output: {out!r}") from exc

    def list_titles(self, ref: DeviceRef) -> list[dict[str, Any]]:
        device = self.resolve_device(ref)
        out, _, _ = self.simple_ssh(device, "python3 ~/devkit-utils/steamos-list-games", check_status=True)
        return json.loads(out)

    def validate_upload_plan(self, profile: UploadProfile) -> UploadPlan:
        root = Path(profile.local_dir).expanduser()
        warnings: list[str] = []
        file_count = 0
        total_bytes = 0
        exists = root.is_dir()
        if not exists:
            warnings.append(f"Source directory does not exist: {root}")
        else:
            for path in root.rglob("*"):
                if path.is_file():
                    file_count += 1
                    with contextlib.suppress(OSError):
                        total_bytes += path.stat().st_size
        if profile.filter_args:
            warnings.append("rsync filter args are not fully simulated in file counts")
        if profile.delete_extraneous:
            warnings.append("clean upload will delete remote files excluded from the new upload")
        if profile.gameid in SIDELOAD_GAMEIDS:
            warnings.append("sideloaded Steam client IDs use special shortcut behavior")
        return UploadPlan(
            profile=profile,
            exists=exists,
            file_count=file_count,
            total_bytes=total_bytes,
            warnings=warnings,
            destructive=profile.delete_extraneous,
            rsync_filter_args=list(profile.filter_args),
        )

    def inspect_android_apk(self, apk_path: str) -> dict[str, Any]:
        apk = Path(apk_path).expanduser()
        if not apk.is_file():
            raise DevkitAdapterError(f"APK not found: {apk}")
        aapt = self.layout.locate_aapt()
        if not aapt:
            raise DevkitAdapterError(
                "aapt was not found. Install Android SDK Build Tools or set AAPT_PATH."
            )
        proc = subprocess.run(
            [aapt, "dump", "badging", str(apk)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            check=False,
        )
        if proc.returncode != 0:
            raise DevkitAdapterError(proc.stderr or proc.stdout or "aapt dump badging failed")
        match = APK_BADGING_PACKAGE_RE.search(proc.stdout)
        if not match:
            raise DevkitAdapterError(f"Could not parse package metadata from aapt output: {proc.stdout}")
        package_name = match.group("name")
        version_code = match.group("version_code")
        return {
            "apk_path": str(apk),
            "aapt_path": aapt,
            "package_name": package_name,
            "version_code": version_code,
            "version_name": match.group("version_name"),
            "expected_main_obb": self.expected_android_main_obb_name(package_name, version_code),
            "badging": proc.stdout,
        }

    @staticmethod
    def expected_android_main_obb_name(package_name: str, version_code: str | int) -> str:
        package = str(package_name).strip()
        version = str(version_code).strip()
        if not ANDROID_PACKAGE_RE.fullmatch(package):
            raise DevkitAdapterError(f"Invalid Android package name: {package_name}")
        if not version.isdigit():
            raise DevkitAdapterError(f"Invalid Android versionCode: {version_code}")
        return f"main.{version}.{package}.obb"

    def validate_android_split_package(
        self,
        local_dir: str,
        apk_name: str | None = None,
    ) -> dict[str, Any]:
        root = Path(local_dir).expanduser()
        if not root.is_dir():
            raise DevkitAdapterError(f"Source directory does not exist: {root}")
        apk = self._select_top_level_apk(root, apk_name)
        apk_info = self.inspect_android_apk(str(apk))
        expected_name = apk_info["expected_main_obb"]
        expected_path = root / "obb" / expected_name
        root_obbs = sorted(root.glob("*.obb"))
        obb_dir = root / "obb"
        nested_obbs = sorted(obb_dir.glob("*.obb")) if obb_dir.is_dir() else []
        all_obbs = root_obbs + nested_obbs

        warnings: list[str] = []
        errors: list[str] = []
        suggestions: list[str] = []
        unity_export_names = [path for path in root_obbs if path.name.endswith(".main.obb")]

        if not all_obbs:
            warnings.append("No OBB files were found; this may be a no-split APK.")
        if unity_export_names:
            names = ", ".join(path.name for path in unity_export_names)
            warnings.append(
                "Unity export-style OBB name found at the package root: "
                f"{names}. Unity/Android expects the Google expansion filename."
            )
        if nested_obbs and expected_path not in nested_obbs:
            errors.append(
                "An obb/ directory exists, but it does not contain the expected main expansion "
                f"file: {expected_name}"
            )
        if root_obbs and expected_path not in nested_obbs:
            suggestions.append(
                f"Stage the main OBB as obb/{expected_name}; Steam Frame Lepton points "
                "/sdcard/Android/obb/<package> at obb/ when that directory is non-empty."
            )
        if expected_path.is_file():
            suggestions.append("Expected Unity/Android OBB filename is already staged under obb/.")
        elif all_obbs:
            errors.append(f"Expected OBB is missing: {expected_path}")

        return {
            "ok": not errors,
            "local_dir": str(root),
            "apk": str(apk),
            "apk_info": apk_info,
            "expected_obb_name": expected_name,
            "expected_obb_path": str(expected_path),
            "root_obbs": [str(path) for path in root_obbs],
            "obb_dir_obbs": [str(path) for path in nested_obbs],
            "warnings": warnings,
            "errors": errors,
            "suggestions": suggestions,
        }

    def stage_android_obb_layout(
        self,
        local_dir: str,
        apk_name: str | None = None,
        source_obb: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        root = Path(local_dir).expanduser()
        validation = self.validate_android_split_package(str(root), apk_name)
        expected_path = Path(validation["expected_obb_path"])
        if source_obb:
            source = Path(source_obb).expanduser()
        else:
            candidates = [Path(path) for path in validation["root_obbs"] + validation["obb_dir_obbs"]]
            candidates = [path for path in candidates if path != expected_path]
            if not candidates:
                raise DevkitAdapterError("No source OBB found to stage")
            source = candidates[0]
        if not source.is_file():
            raise DevkitAdapterError(f"Source OBB not found: {source}")
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        if expected_path.exists() and not overwrite:
            if _file_sha256(source) != _file_sha256(expected_path):
                raise DevkitAdapterError(
                    f"Expected OBB already exists and differs: {expected_path}. "
                    "Pass overwrite=True to replace it."
                )
            copied = False
        else:
            shutil.copy2(source, expected_path)
            copied = True
        return {
            "copied": copied,
            "source_obb": str(source),
            "staged_obb": str(expected_path),
            "validation": self.validate_android_split_package(str(root), apk_name),
        }

    def steampipe_android_release_preflight(
        self,
        local_dir: str,
        apk_name: str | None = None,
        app_id: str | None = None,
        depot_id: str | None = None,
        launch_executable: str | None = None,
        cloud_subdirectory: str | None = None,
    ) -> dict[str, Any]:
        root = Path(local_dir).expanduser()
        errors: list[str] = []
        warnings: list[str] = []
        manual: list[str] = []
        if not root.is_dir():
            raise DevkitAdapterError(f"Source directory does not exist: {root}")

        apk: Path | None = None
        apk_info: dict[str, Any] | None = None
        with contextlib.suppress(DevkitAdapterError):
            apk = self._select_top_level_apk(root, apk_name)
        top_level_apks = sorted(root.glob("*.apk"))
        nested_apks = sorted(path for path in root.rglob("*.apk") if path.parent != root)
        root_obbs = sorted(root.glob("*.obb"))
        obb_dir = root / "obb"
        obb_files = sorted(obb_dir.rglob("*.obb")) if obb_dir.is_dir() else []

        if apk is None:
            if not top_level_apks:
                errors.append("Steam Android depots need at least one top-level APK executable.")
            else:
                errors.append("Multiple top-level APKs found; pass apk_name or launch_executable.")
        else:
            with contextlib.suppress(DevkitAdapterError):
                apk_info = self.inspect_android_apk(str(apk))
        if nested_apks:
            warnings.append("Nested APKs were found; Steam launch options expect a top-level APK executable.")
        if root_obbs:
            warnings.append("Root-level OBB files were found; Steam's Android layout expects OBBs under obb/.")
        if obb_dir.exists() and not obb_dir.is_dir():
            errors.append("obb exists but is not a directory.")

        executable = launch_executable or (apk.name if apk else None)
        if executable:
            executable_path = root / executable
            if executable_path.parent != root or executable_path.suffix.lower() != ".apk":
                errors.append("Launch executable should be a top-level .apk path relative to the depot root.")
            elif not executable_path.is_file():
                errors.append(f"Launch executable was not found under depot root: {executable}")
        else:
            manual.append("Set a Steamworks Android launch option whose executable is the top-level APK.")

        checklist = [
            {
                "item": "Application > General has Android checked under Supported Operating Systems",
                "status": "manual",
                "evidence": {"app_id": app_id},
            },
            {
                "item": "SteamPipe depot used for this build has Operating System set to Android",
                "status": "manual",
                "evidence": {"depot_id": depot_id},
            },
            {
                "item": "Depot root contains a top-level APK for the Android launch option",
                "status": "ok" if apk and not errors else "error",
                "evidence": {"apk": str(apk) if apk else None, "top_level_apks": [path.name for path in top_level_apks]},
            },
            {
                "item": "Extra Android content is under obb/",
                "status": "ok" if not root_obbs else "warning",
                "evidence": {
                    "root_obbs": [path.name for path in root_obbs],
                    "obb_files": [str(path.relative_to(root)) for path in obb_files],
                },
            },
            {
                "item": "Steamworks launch option uses Operating System Android",
                "status": "manual",
                "evidence": {"launch_executable": executable},
            },
            {
                "item": "Developer Comp or test package grants access to the Android depot",
                "status": "manual",
                "evidence": {"depot_id": depot_id},
            },
            {
                "item": "Steam Cloud uses AndroidExternalData if Android saves need cloud sync",
                "status": "manual" if not cloud_subdirectory else "ok",
                "evidence": {"cloud_subdirectory": cloud_subdirectory},
            },
        ]

        return {
            "ok": not errors,
            "local_dir": str(root),
            "app_id": app_id,
            "depot_id": depot_id,
            "launch_executable": executable,
            "apk_info": apk_info,
            "errors": errors,
            "warnings": warnings,
            "manual_checks": manual,
            "checklist": checklist,
            "docs": [
                "steamworks_docs_md/docs/steamhardware/steamframe/apk_upload.md",
                "steamworks_docs_md/docs/sdk/uploading.md",
            ],
        }

    def upload_title(self, profile: UploadProfile) -> dict[str, Any]:
        plan = self.validate_upload_plan(profile)
        if not plan.exists:
            raise DevkitAdapterError(plan.warnings[0])
        device = self.resolve_device(profile.device)
        prepare_cmd = f"python3 ~/devkit-utils/steamos-prepare-upload --gameid {shlex.quote(profile.gameid)}"
        if profile.restart_steam:
            prepare_cmd += " --restart-steam 1"
        if profile.use_mask_unmask:
            prepare_cmd += " --use-mask-unmask 1"
        if profile.prevent_auto_repair:
            prepare_cmd += " --prevent-auto-repair 1"
        out, _, _ = self.simple_ssh(device, prepare_cmd, check_status=True)
        prep = json.loads(out)
        remote_user = prep["user"]
        destdir = prep["directory"]
        self.rsync_transfer(
            profile.local_dir,
            device,
            destdir,
            upload=True,
            remote_user=remote_user,
            delete_extraneous=profile.delete_extraneous,
            skip_newer_files=profile.skip_newer_files,
            verify_checksums=profile.verify_checksums,
            extra_cmdline=profile.filter_args,
        )
        shortcut_payload = self._shortcut_payload(profile, destdir)
        if profile.gameid in SIDELOAD_GAMEIDS:
            self._write_sideload_settings(device, shortcut_payload)
            shortcut = {"success": "sideload settings written"}
        else:
            remote_cmd = (
                "python3 ~/devkit-utils/steam-client-create-shortcut --parms "
                + shlex.quote(json.dumps(shortcut_payload))
            )
            out, _, _ = self.simple_ssh(device, remote_cmd, check_status=True)
            shortcut = json.loads(out)
            if "error" in shortcut:
                raise DevkitAdapterError(shortcut["error"])
        return {
            "device": to_jsonable(device),
            "gameid": profile.gameid,
            "remote_directory": destdir,
            "plan": to_jsonable(plan),
            "shortcut": shortcut,
        }

    def run_title(self, ref: DeviceRef, gameid: str) -> dict[str, Any]:
        return self.steam_rpc(ref, "run-game", {"gameid": gameid})

    def delete_title(
        self,
        ref: DeviceRef,
        gameid: str | None = None,
        delete_all: bool = False,
        reset_steam_client: bool = False,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        cmd = ["python3", "~/devkit-utils/steamos-delete"]
        if gameid:
            cmd += ["--delete-title", shlex.quote(gameid)]
        if delete_all:
            cmd.append("--delete-all-titles")
        if reset_steam_client:
            cmd.append("--reset-steam-client")
        out, err, status = self.simple_ssh(device, " ".join(cmd))
        if status != 0:
            raise DevkitAdapterError(err or out)
        return {"stdout": out, "stderr": err, "device": to_jsonable(device)}

    def set_steam_client(
        self,
        ref: DeviceRef,
        gameid: str,
        mode: str,
        args: str | None = None,
        gdbserver: bool = False,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        config = _normalize_steam_client_mode(mode)
        cmd = (
            "python3 ~/devkit-utils/steamos-set-steam-client "
            f"--client {config} --gameid {shlex.quote(gameid)}"
        )
        if args is not None:
            cmd += f" --args={shlex.quote(args)}"
        if gdbserver:
            cmd += " --gdbserver"
        out, err, status = self.simple_ssh(device, cmd)
        if status != 0:
            raise DevkitAdapterError(err or out or "set steam client failed")
        return {"device": to_jsonable(device), "mode": config, "stdout": out, "stderr": err}

    def set_session(self, ref: DeviceRef, session: str, wait: bool = True) -> dict[str, Any]:
        device = self.resolve_device(ref)
        select_arg = "plasma" if session in {"plasma-x11", "plasma-wayland"} else session
        if session == "plasma-wayland":
            self.simple_ssh(device, 'echo wayland > ${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type')
        else:
            self.simple_ssh(device, 'rm -f ${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type')
        self.simple_ssh(device, f"steamos-session-select {shlex.quote(select_arg)}", check_status=True)
        status = None
        if wait:
            for _ in range(5):
                time.sleep(1)
                with contextlib.suppress(Exception):
                    status = self.get_steamos_status(ref)
                    if status.get("session_status") == session:
                        break
        return {"device": to_jsonable(device), "session": session, "status": status}

    def restart_session(self, ref: DeviceRef, is_deckard: bool | None = None) -> dict[str, Any]:
        device = self.resolve_device(ref)
        if is_deckard is None:
            with contextlib.suppress(Exception):
                is_deckard = bool(self.get_steamos_status(ref).get("is_deckard"))
        cmd = "systemctl --user unmask steam ; systemctl --user restart steam" if is_deckard else RESTART_SESSION
        out, err, status = self.simple_ssh(device, cmd)
        if status != 0:
            raise DevkitAdapterError(err or out)
        return {"device": to_jsonable(device), "stdout": out, "stderr": err}

    def reboot_device(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        out, err, status = self.simple_ssh(device, REBOOT_NOW)
        if status != 0:
            raise DevkitAdapterError(err or out)
        return {"device": to_jsonable(device), "stdout": out, "stderr": err}

    def enable_cef_debugging(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        self.simple_ssh(device, "touch ~/.steam/steam/.cef-enable-remote-debugging", check_status=True)
        return {"device": to_jsonable(device), "url": f"http://{device.address}:8081"}

    def open_cef_console(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref, refresh_properties=False)
        return {"device": to_jsonable(device), "url": f"http://{device.address}:8081"}

    def steam_rpc(self, ref: DeviceRef, command: str, params: dict[str, Any]) -> dict[str, Any]:
        device = self.resolve_device(ref)
        parts = ["python3", "~/devkit-utils/steam-devkit-rpc", shlex.quote(command)]
        for key, value in params.items():
            parts.append(shlex.quote(f"{key}={value}"))
        out, err, status = self.simple_ssh(device, " ".join(parts))
        if status != 0:
            raise DevkitAdapterError(err or out)
        return {"device": to_jsonable(device), "stdout": out, "stderr": err}

    def run_remote_command(self, ref: DeviceRef, command: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        out, err, status = self.simple_ssh(device, command)
        return {"device": to_jsonable(device), "exit_status": status, "stdout": out, "stderr": err}

    def dump_controller_config(
        self,
        ref: DeviceRef,
        output_folder: str,
        appid: str | None = None,
        gameid: str | None = None,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        self.simple_ssh(device, "rm -f /tmp/config_*.tmp /tmp/config_*.vdf", silent=True)
        cmd = ["python3", "~/devkit-utils/steamos-dump-controller-config"]
        if appid:
            cmd += ["--appid", shlex.quote(appid)]
        elif gameid:
            cmd += ["--gameid", shlex.quote(gameid)]
        out, _, _ = self.simple_ssh(device, " ".join(cmd), check_status=True)
        result = json.loads(out)
        if "error" in result:
            raise DevkitAdapterError(result["error"])
        self.rsync_transfer(
            output_folder,
            device,
            "/tmp",
            upload=False,
            extra_cmdline=["--include=config_*.vdf", "--exclude=*"],
        )
        return {"device": to_jsonable(device), "output_folder": output_folder, "steam_response": result}

    def sync_logs(
        self,
        ref: DeviceRef,
        local_folder: str,
        steamvr_logpath: str | None = None,
        device_name: str | None = None,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        if not device.login:
            raise DevkitAdapterError("Device login is required for log sync")
        steam_logs_path = REMOTE_STEAM_LOGS_PATH.format(login=device.login)
        sources: list[str] = []
        if steamvr_logpath and steamvr_logpath != steam_logs_path:
            sources.append(steamvr_logpath)
        sources += [steam_logs_path, "/tmp/dumps"]
        local_device_folder = Path(local_folder) / (device_name or device.name or device.id)
        self.rsync_transfer(str(local_device_folder), device, sources, upload=False)
        return {"device": to_jsonable(device), "local_folder": str(local_device_folder), "sources": sources}

    def lepton_cli_help(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        command = (
            'LEPTON="$HOME/.local/share/Steam/steamapps/common/Lepton/lepton"; '
            'if [ -x "$LEPTON" ]; then "$LEPTON" help; else echo "Lepton CLI not found: $LEPTON"; exit 127; fi'
        )
        out, err, status = self.simple_ssh(device, command, silent=True)
        return {"device": to_jsonable(device), "exit_status": status, "stdout": out, "stderr": err}

    def lepton_containers(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import subprocess
import sys
import os


def run(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


ps = run(["podman", "ps", "-a", "--filter", "label=lepton=true", "--format", "{{.Names}}"])
result = {"containers": [], "stderr": ps.stderr.strip(), "returncode": ps.returncode}
if ps.returncode != 0:
    print(json.dumps(result))
    sys.exit(0)

for name in [line.strip() for line in ps.stdout.splitlines() if line.strip()]:
    inspect = run(["podman", "inspect", name])
    if inspect.returncode != 0:
        result["containers"].append(
            {"name": name, "inspect_error": inspect.stderr.strip(), "inspect_returncode": inspect.returncode}
        )
        continue
    try:
        payload = json.loads(inspect.stdout)
    except json.JSONDecodeError as exc:
        result["containers"].append({"name": name, "inspect_error": str(exc)})
        continue
    item = payload[0] if isinstance(payload, list) and payload else payload
    labels = ((item.get("Config") or {}).get("Labels") or {})
    state = item.get("State") or {}
    clean_name = (item.get("Name") or name).lstrip("/")
    context = clean_name.removeprefix("lepton-")
    home = os.path.expanduser("~")
    result["containers"].append(
        {
            "name": clean_name,
            "context": context,
            "id": item.get("Id"),
            "status": state.get("Status"),
            "running": bool(state.get("Running")),
            "pid": state.get("Pid"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "image": item.get("ImageName") or ((item.get("Config") or {}).get("Image")),
            "labels": labels,
            "ports": {
                "adb": labels.get("adb_port"),
                "gdb": labels.get("gdb_port"),
                "lldb": labels.get("lldb_port"),
            },
            "prefix": labels.get("PREFIX"),
            "steam_compat_data_path": labels.get("STEAM_COMPAT_DATA_PATH"),
            "lepton_pid": labels.get("lepton_pid"),
            "log_file": f"{home}/.local/share/Steam/logs/lepton-{context}.log",
        }
    )

print(json.dumps(result))
""",
        )
        for container in data.get("containers", []):
            adb_port = container.get("ports", {}).get("adb")
            if adb_port and device.address:
                container["adb_target"] = f"{device.address}:{adb_port}"
        return {"device": to_jsonable(device), **data}

    def lepton_logcat(self, ref: DeviceRef, context: str = "dev", lines: int = 300) -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = context.strip()
        if not LEPTON_CONTEXT_RE.fullmatch(clean_context):
            raise DevkitAdapterError(f"Invalid Lepton context name: {context}")
        bounded_lines = min(max(int(lines), 1), 5000)
        command = (
            'LEPTON="$HOME/.local/share/Steam/steamapps/common/Lepton/lepton"; '
            'if [ ! -x "$LEPTON" ]; then echo "Lepton CLI not found: $LEPTON" >&2; exit 127; fi; '
            f'timeout 8s "$LEPTON" logcat {shlex.quote(clean_context)} 2>&1 | tail -n {bounded_lines}'
        )
        out, err, status = self.simple_ssh(device, command, silent=True)
        return {
            "device": to_jsonable(device),
            "context": clean_context,
            "exit_status": status,
            "stderr": err,
            "lines": out.splitlines(),
            "highlight_lines": _matching_lines(out, ADB_LOG_HIGHLIGHT_RE, limit=250),
        }

    def steam_logs_manifest(
        self,
        ref: DeviceRef,
        pattern: str | None = None,
        limit: int = 100,
        include_tmp: bool = False,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        bounded_limit = min(max(int(limit), 1), 500)
        data = self._remote_python_json(
            device,
            "PATTERN = "
            + json.dumps(pattern or "")
            + "\nLIMIT = "
            + str(bounded_limit)
            + "\nINCLUDE_TMP = "
            + ("True" if include_tmp else "False")
            + r"""

import fnmatch
import json
import os
import time

home = os.path.expanduser("~")
roots = [os.path.join(home, ".local/share/Steam/logs")]
if INCLUDE_TMP:
    roots.append("/tmp")

prefixes = (
    "lepton",
    "xrclient",
    "vrclient",
    "vrserver",
    "vrcompositor",
    "steam",
    "gameprocess",
    "compat",
)
extensions = (".log", ".txt", ".pftrace", ".dmp", ".mdmp", ".zip")


def kind_for(name):
    lowered = name.lower()
    if lowered.startswith("lepton"):
        return "lepton"
    if lowered.startswith(("xrclient", "vrclient")):
        return "openxr"
    if lowered.startswith(("vrserver", "vrcompositor")):
        return "steamvr"
    if lowered.startswith("steam") or lowered in {"compat_log.txt", "gameprocess_log.txt"}:
        return "steam"
    if lowered.endswith((".pftrace", ".rgp")):
        return "trace"
    if lowered.endswith((".dmp", ".mdmp")):
        return "dump"
    return "other"


def interesting(path, name):
    if PATTERN:
        lowered_path = path.lower()
        lowered_name = name.lower()
        lowered_pattern = PATTERN.lower()
        return (
            fnmatch.fnmatch(name, PATTERN)
            or fnmatch.fnmatch(path, PATTERN)
            or lowered_pattern in lowered_name
            or lowered_pattern in lowered_path
        )
    lowered = name.lower()
    return lowered.startswith(prefixes) or lowered.endswith(extensions)


entries = []
for root in roots:
    if not os.path.exists(root):
        continue
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 3:
            dirnames[:] = []
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not interesting(path, name):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append(
                {
                    "path": path,
                    "name": name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)),
                    "kind": kind_for(name),
                }
            )

entries.sort(key=lambda item: item["mtime"], reverse=True)
print(json.dumps({"entries": entries[:LIMIT], "roots": roots, "pattern": PATTERN, "limit": LIMIT}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def steam_frame_perfcriteria(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import os
import re
from pathlib import Path

roots = [
    Path(os.path.expanduser("~")) / ".local/share/Steam/logs",
    Path(os.path.expanduser("~")) / ".local/share/Steam",
    Path("/tmp"),
]
files = []
for root in roots:
    if not root.exists():
        continue
    try:
        for path in root.rglob("perfcriteria.txt"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({"path": str(path), "mtime": stat.st_mtime, "size": stat.st_size})
    except OSError:
        continue

files.sort(key=lambda item: item["mtime"], reverse=True)
latest = files[0] if files else None
parsed = {}
lines = []
if latest:
    try:
        text = Path(latest["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        parsed["error"] = str(exc)
    else:
        lines = text.splitlines()
        parsed["summary"] = lines[:3]
        frame_time_lines = [
            line for line in lines if re.search(r"frame|ms|threshold|violation|resolution|fps", line, re.I)
        ]
        parsed["frame_time_lines"] = frame_time_lines[:120]

print(json.dumps({"files": files[:20], "latest": latest, "parsed": parsed, "lines": lines[:200]}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "Steam Frame docs describe perfcriteria.txt as the source for app/appid/target/"
                "effective resolution and frame-time threshold violations.",
                "Frame compatibility targets 72 fps at 1728x1728; below 1440x1440 is documented as Unsupported.",
            ],
        }

    def steam_frame_cef_pages(self, ref: DeviceRef, ports: list[int] | None = None) -> dict[str, Any]:
        device = self.resolve_device(ref)
        selected_ports = ports or [8081, 8088]
        if any(port < 1 or port > 65535 for port in selected_ports):
            raise DevkitAdapterError("CEF ports must be between 1 and 65535")
        data = self._remote_python_json(
            device,
            "PORTS = " + json.dumps(selected_ports) + r"""

import json
import subprocess
import urllib.error
import urllib.request


def fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode("utf-8", "replace")), None
    except Exception as exc:
        return None, str(exc)


results = []
for port in PORTS:
    base = f"http://127.0.0.1:{port}"
    version, version_error = fetch_json(base + "/json/version")
    pages, pages_error = fetch_json(base + "/json/list")
    if isinstance(pages, list):
        pages = [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "title": item.get("title"),
                "url": item.get("url"),
                "devtoolsFrontendUrl": item.get("devtoolsFrontendUrl"),
                "webSocketDebuggerUrl": item.get("webSocketDebuggerUrl"),
            }
            for item in pages
        ]
    results.append(
        {
            "port": port,
            "version": version,
            "version_error": version_error,
            "pages": pages or [],
            "pages_error": pages_error,
        }
    )

print(json.dumps({"cef": results}))
""",
        )
        for item in data.get("cef", []):
            port = item.get("port")
            if device.address and port:
                item["url"] = f"http://{device.address}:{port}"
        return {"device": to_jsonable(device), **data}

    def steam_frame_web_ports(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import re
import subprocess
import urllib.request

ports = [32000, 8081, 8088, 27060, 5555, 5556]
proc = subprocess.run(["ss", "-ltnp"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
listeners = []
for line in proc.stdout.splitlines():
    if not any(f":{port}" in line for port in ports):
        continue
    port = None
    match = re.search(r":(\d+)\s+", line)
    if match:
        port = int(match.group(1))
    listeners.append({"port": port, "line": line})

devkit_properties = None
devkit_error = None
try:
    with urllib.request.urlopen("http://127.0.0.1:32000/properties.json", timeout=3) as response:
        devkit_properties = json.loads(response.read().decode("utf-8", "replace"))
except Exception as exc:
    devkit_error = str(exc)

print(
    json.dumps(
        {
            "listeners": listeners,
            "ss_returncode": proc.returncode,
            "ss_stderr": proc.stderr.strip(),
            "devkit_properties": devkit_properties,
            "devkit_error": devkit_error,
        }
    )
)
""",
        )
        if device.address:
            for item in data.get("listeners", []):
                port = item.get("port")
                if port:
                    item["url"] = f"http://{device.address}:{port}"
        return {"device": to_jsonable(device), **data}

    def steam_frame_dbus_manager(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import re
import subprocess

SERVICE = "com.steampowered.SteamOSManager1"
PATH = "/com/steampowered/SteamOSManager1"

proc = subprocess.run(
    ["busctl", "--user", "introspect", "--no-pager", SERVICE, PATH],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
interfaces = []
current = None
for line in proc.stdout.splitlines():
    if line.startswith("NAME ") or not line.strip():
        continue
    parts = line.split(None, 4)
    if len(parts) < 2:
        continue
    name, kind = parts[0], parts[1]
    if kind == "interface":
        current = {"name": name, "properties": [], "methods": [], "signals": []}
        interfaces.append(current)
        continue
    if current is None:
        continue
    row = {"name": name.lstrip("."), "signature": parts[2] if len(parts) > 2 else "", "raw": line}
    if kind == "property":
        value_flags = parts[4] if len(parts) > 4 else ""
        row["value"] = value_flags
        row["writable"] = "writable" in value_flags
        current["properties"].append(row)
    elif kind == "method":
        row["result"] = parts[3] if len(parts) > 3 else ""
        current["methods"].append(row)
    elif kind == "signal":
        current["signals"].append(row)

print(
    json.dumps(
        {
            "service": SERVICE,
            "path": PATH,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "interfaces": interfaces,
            "raw_lines": proc.stdout.splitlines()[:400],
            "notes": [
                "Properties are read-only here even when DBus marks them writable.",
                "Manager control methods and writable properties should be separate confirmation-gated tools.",
            ],
        }
    )
)
""",
        )
        return {"device": to_jsonable(device), **data}

    def native_adbd_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import subprocess

props = ["ActiveState", "SubState", "LoadState", "FragmentPath", "MainPID", "Environment"]
proc = subprocess.run(
    ["systemctl", "show", "adbd.service", "--no-pager"] + [f"-p{name}" for name in props],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
status = {}
for line in proc.stdout.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        status[key] = value
print(json.dumps({"unit": "adbd.service", "returncode": proc.returncode, "stderr": proc.stderr.strip(), "status": status}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def coredump_list(self, ref: DeviceRef, limit: int = 20) -> dict[str, Any]:
        device = self.resolve_device(ref)
        bounded_limit = min(max(int(limit), 1), 100)
        data = self._remote_python_json(
            device,
            "LIMIT = " + str(bounded_limit) + r"""

import json
import subprocess

proc = subprocess.run(
    ["coredumpctl", "--no-pager", "--no-legend", "list"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
    timeout=20,
)
entries = []
for line in proc.stdout.splitlines()[-LIMIT:]:
    parts = line.split()
    exe = parts[-2] if len(parts) >= 2 and parts[-1] == "-" else parts[-1] if parts else ""
    entries.append({"line": line, "executable": exe})
print(json.dumps({"entries": entries, "returncode": proc.returncode, "stderr": proc.stderr.strip(), "limit": LIMIT}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "Use coredumpctl debug <PID> manually or through a future confirmation-gated tool for backtraces.",
                "Some dumps may be inaccessible without elevated permissions.",
            ],
        }

    def lepton_context_inspect(
        self,
        ref: DeviceRef,
        context: str,
        include_mounts: bool = False,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = self._validate_lepton_context(context)
        data = self._remote_python_json(
            device,
            "CONTEXT = "
            + json.dumps(clean_context)
            + "\nINCLUDE_MOUNTS = "
            + ("True" if include_mounts else "False")
            + r"""

import json
import subprocess

name = f"lepton-{CONTEXT}"
proc = subprocess.run(["podman", "inspect", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
if proc.returncode != 0:
    print(json.dumps({"context": CONTEXT, "name": name, "exists": False, "stderr": proc.stderr.strip(), "returncode": proc.returncode}))
    raise SystemExit(0)
payload = json.loads(proc.stdout)
item = payload[0] if isinstance(payload, list) and payload else payload
labels = ((item.get("Config") or {}).get("Labels") or {})
state = item.get("State") or {}
network = ((item.get("NetworkSettings") or {}).get("Networks") or {})
mounts = []
if INCLUDE_MOUNTS:
    for mount in item.get("Mounts") or []:
        mounts.append(
            {
                "type": mount.get("Type"),
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
                "mode": mount.get("Mode"),
                "rw": mount.get("RW"),
            }
        )
print(
    json.dumps(
        {
            "context": CONTEXT,
            "name": (item.get("Name") or name).lstrip("/"),
            "exists": True,
            "id": item.get("Id"),
            "status": state.get("Status"),
            "running": bool(state.get("Running")),
            "pid": state.get("Pid"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "labels": labels,
            "ports": {"adb": labels.get("adb_port"), "gdb": labels.get("gdb_port"), "lldb": labels.get("lldb_port")},
            "prefix": labels.get("PREFIX"),
            "steam_compat_data_path": labels.get("STEAM_COMPAT_DATA_PATH"),
            "lepton_pid": labels.get("lepton_pid"),
            "network": network,
            "mounts": mounts,
        }
    )
)
""",
        )
        ports = data.get("ports") or {}
        for key, port in ports.items():
            if port and device.address:
                data[f"{key}_target"] = f"{device.address}:{port}"
        return {"device": to_jsonable(device), **data}

    def lepton_debug_targets(self, ref: DeviceRef, context: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = self._validate_lepton_context(context)
        data = self._remote_python_json(
            device,
            "CONTEXT = " + json.dumps(clean_context) + r"""

import json
import re
import subprocess

name = f"lepton-{CONTEXT}"
inspect = subprocess.run(["podman", "inspect", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
result = {"context": CONTEXT, "exists": inspect.returncode == 0, "inspect_stderr": inspect.stderr.strip()}
ports = {}
if inspect.returncode == 0:
    payload = json.loads(inspect.stdout)
    item = payload[0] if isinstance(payload, list) and payload else payload
    labels = ((item.get("Config") or {}).get("Labels") or {})
    ports = {"adb": labels.get("adb_port"), "gdb": labels.get("gdb_port"), "lldb": labels.get("lldb_port")}
result["ports"] = ports
ss = subprocess.run(["ss", "-ltnp"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
listeners = []
for line in ss.stdout.splitlines():
    match = re.search(r":(\d+)\s+", line)
    if not match:
        continue
    port = match.group(1)
    if port in {str(value) for value in ports.values() if value}:
        listeners.append({"port": port, "line": line})
result["listeners"] = listeners
result["ss_returncode"] = ss.returncode
result["ss_stderr"] = ss.stderr.strip()
print(json.dumps(result))
""",
        )
        for key, port in (data.get("ports") or {}).items():
            if port and device.address:
                data[f"{key}_target"] = f"{device.address}:{port}"
        data["notes"] = [
            "This reports existing labels/listeners only.",
            "Starting or killing gdb/lldb servers should be a separate confirmation-gated action.",
        ]
        return {"device": to_jsonable(device), **data}

    def lepton_mounts(self, ref: DeviceRef, context: str, category: str = "all") -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = self._validate_lepton_context(context)
        categories = {"all", "obb", "steamvr", "steamlibs", "openxr", "vulkan", "debugfs", "storage"}
        clean_category = category.lower()
        if clean_category not in categories:
            raise DevkitAdapterError(f"category must be one of: {', '.join(sorted(categories))}")
        data = self._remote_python_json(
            device,
            "CONTEXT = " + json.dumps(clean_context) + "\nCATEGORY = " + json.dumps(clean_category) + r"""

import json
import subprocess


def classify(mount):
    text = " ".join(str(mount.get(key) or "") for key in ("Source", "Destination", "Name")).lower()
    if "obb" in text or "android/obb" in text:
        return "obb"
    if "steamvr" in text:
        return "steamvr"
    if "steamapps" in text or "steamrt" in text or "steam" in text:
        return "steamlibs"
    if "openxr" in text:
        return "openxr"
    if "vulkan" in text:
        return "vulkan"
    if "debug" in text or "trace" in text:
        return "debugfs"
    if "/data" in text or "/sdcard" in text or "media/0" in text or "storage" in text:
        return "storage"
    return "other"


name = f"lepton-{CONTEXT}"
proc = subprocess.run(["podman", "inspect", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
if proc.returncode != 0:
    print(json.dumps({"context": CONTEXT, "exists": False, "stderr": proc.stderr.strip(), "returncode": proc.returncode, "mounts": []}))
    raise SystemExit(0)
payload = json.loads(proc.stdout)
item = payload[0] if isinstance(payload, list) and payload else payload
mounts = []
for mount in item.get("Mounts") or []:
    kind = classify(mount)
    if CATEGORY != "all" and kind != CATEGORY:
        continue
    mounts.append(
        {
            "category": kind,
            "type": mount.get("Type"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "mode": mount.get("Mode"),
            "rw": mount.get("RW"),
            "propagation": mount.get("Propagation"),
        }
    )
labels = ((item.get("Config") or {}).get("Labels") or {})
print(json.dumps({"context": CONTEXT, "exists": True, "category": CATEGORY, "labels": labels, "mounts": mounts}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def lepton_apk_info(self, ref: DeviceRef, apk_path: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        if not apk_path.strip():
            raise DevkitAdapterError("apk_path is required")
        data = self._remote_python_json(
            device,
            "APK_PATH = " + json.dumps(apk_path.strip()) + r"""

import json
import os
import subprocess
from pathlib import Path

home = os.path.expanduser("~")
apk = APK_PATH.replace("~", home, 1) if APK_PATH == "~" or APK_PATH.startswith("~/") else APK_PATH
extractor = os.path.join(home, ".local/share/Steam/steamapps/common/Lepton/liblepton/apk_extractor/bin/apk-info-extractor")
result = {"apk_path": apk, "extractor": extractor, "exists": os.path.isfile(apk), "fields": {}}
if not result["exists"]:
    print(json.dumps(result))
    raise SystemExit(0)
if not os.path.isfile(extractor):
    result["error"] = "apk-info-extractor not found"
    print(json.dumps(result))
    raise SystemExit(0)
for key, flag in {
    "package_name": "--print-app-id",
    "activity_name": "--print-activity-name",
    "version_code": "--print-app-version",
}.items():
    proc = subprocess.run([extractor, flag, apk], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=20)
    result["fields"][key] = {"returncode": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}
result["expected_main_obb"] = None
package = result["fields"].get("package_name", {}).get("stdout")
version = result["fields"].get("version_code", {}).get("stdout")
if package and version:
    result["expected_main_obb"] = f"main.{version}.{package}.obb"
print(json.dumps(result))
""",
        )
        return {"device": to_jsonable(device), **data}

    def lepton_rootfs_overlay_manifest(
        self,
        ref: DeviceRef,
        max_depth: int = 4,
        include_snippets: bool = True,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        bounded_depth = min(max(int(max_depth), 1), 8)
        data = self._remote_python_json(
            device,
            "MAX_DEPTH = "
            + str(bounded_depth)
            + "\nINCLUDE_SNIPPETS = "
            + ("True" if include_snippets else "False")
            + r"""

import hashlib
import json
import os
from pathlib import Path

root = Path(os.path.expanduser("~")) / ".local/share/Steam/steamapps/common/Lepton/images/rootfs_overlay"
entries = []
if root.exists():
    base_depth = len(root.parts)
    for path in sorted(root.rglob("*")):
        depth = len(path.parts) - base_depth
        if depth > MAX_DEPTH or not path.is_file():
            continue
        try:
            data = path.read_bytes()
            stat = path.stat()
        except OSError:
            continue
        item = {
            "path": str(path),
            "relative_path": str(path.relative_to(root)),
            "size": stat.st_size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if INCLUDE_SNIPPETS and stat.st_size <= 16384:
            try:
                item["snippet"] = data.decode("utf-8", "replace")[:4000]
            except Exception:
                pass
        entries.append(item)
print(json.dumps({"root": str(root), "exists": root.exists(), "entries": entries}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def lepton_debug_plan(self, ref: DeviceRef, context: str, mode: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = self._validate_lepton_context(context)
        clean_mode = mode.lower().strip()
        plans: dict[str, dict[str, Any]] = {
            "gdb": {
                "safety_for_execution": "write",
                "status_tool": "lepton_debug_targets",
                "commands": [
                    f"lepton gdb_server {clean_context}",
                    f"lepton gdb_attach {clean_context}",
                    f"lepton kill_gdb_server {clean_context}",
                ],
                "env": ["LEPTON_DEBUG_LAUNCH"],
            },
            "lldb": {
                "safety_for_execution": "write",
                "status_tool": "lepton_debug_targets",
                "commands": [f"lepton lldb_server {clean_context}", f"lepton kill_lldb_server {clean_context}"],
                "env": ["LEPTON_DEBUG_LAUNCH"],
            },
            "strace": {
                "safety_for_execution": "write",
                "commands": ["launch with LEPTON_STRACE=1", "extract /data/strace-<app-id>.log after exit"],
                "env": ["LEPTON_STRACE", "LEPTON_STRACE_ARGS"],
            },
            "perfetto": {
                "safety_for_execution": "write",
                "commands": [f"lepton perfetto {clean_context}", "download /tmp/lepton-<context>-*.pftrace"],
                "env": ["PERFETTO_CONFIG"],
            },
            "renderdoc": {
                "safety_for_execution": "write",
                "commands": ["launch title with ENABLE_VULKAN_RENDERDOC_CAPTURE=1"],
                "env": ["ENABLE_VULKAN_RENDERDOC_CAPTURE", "VK_INSTANCE_LAYERS"],
            },
            "vulkan_layers": {
                "safety_for_execution": "write",
                "commands": ["launch title with selected Vulkan layer env vars"],
                "env": [
                    "ENABLE_VULKAN_VALIDATION_LAYER",
                    "ENABLE_VULKAN_FDM_INJECTION_LAYER",
                    "ENABLE_VULKAN_RPO_LAYER",
                    "VK_INSTANCE_LAYERS",
                ],
            },
        }
        if clean_mode not in plans:
            raise DevkitAdapterError(f"mode must be one of: {', '.join(sorted(plans))}")
        return {
            "device": to_jsonable(device),
            "context": clean_context,
            "mode": clean_mode,
            "plan": plans[clean_mode],
            "notes": [
                "This is a read-only execution plan extracted from Lepton scripts.",
                "Run/capture/debug lifecycle actions should be separate confirmation-gated tools.",
            ],
        }

    def steam_frame_manager_properties(self, ref: DeviceRef, bus: str = "both") -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_bus = bus.lower()
        if clean_bus not in {"user", "system", "both"}:
            raise DevkitAdapterError("bus must be 'user', 'system', or 'both'")
        data = self._remote_python_json(
            device,
            "BUS = " + json.dumps(clean_bus) + r"""

import json
import os
import subprocess

SERVICE = "com.steampowered.SteamOSManager1"
PATH = "/com/steampowered/SteamOSManager1"


def collect(bus):
    args = ["busctl"]
    if bus == "user":
        args.append("--user")
    args += ["introspect", "--no-pager", SERVICE, PATH]
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, env=env)
    interfaces = {}
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("NAME ") or not line.strip():
            continue
        parts = line.split(None, 4)
        if len(parts) < 2:
            continue
        name, kind = parts[0], parts[1]
        if kind == "interface":
            current = name
            interfaces.setdefault(current, {})
            continue
        if current and kind == "property":
            flags = parts[4] if len(parts) > 4 else ""
            interfaces[current][name.lstrip(".")] = {
                "signature": parts[2] if len(parts) > 2 else "",
                "value": parts[3] if len(parts) > 3 else "",
                "raw": line,
                "flags": flags,
                "writable": "writable" in flags,
                "const": "const" in flags,
            }
    return {"returncode": proc.returncode, "stderr": proc.stderr.strip(), "interfaces": interfaces}


buses = ["user", "system"] if BUS == "both" else [BUS]
print(json.dumps({"service": SERVICE, "path": PATH, "buses": {bus: collect(bus) for bus in buses}}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def steam_frame_manager_interfaces(self, ref: DeviceRef, include_system: bool = True) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            "INCLUDE_SYSTEM = " + ("True" if include_system else "False") + r"""

import json
import os
import subprocess

SERVICE = "com.steampowered.SteamOSManager1"
PATHS = ["/com/steampowered/SteamOSManager1", "/com/steampowered/SteamOSManager1/Jobs"]
KIND_BUCKETS = {"property": "properties", "method": "methods", "signal": "signals"}


def introspect(bus, path):
    args = ["busctl"]
    if bus == "user":
        args.append("--user")
    args += ["introspect", "--no-pager", SERVICE, path]
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, env=env)
    interfaces = []
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("NAME ") or not line.strip():
            continue
        parts = line.split(None, 4)
        if len(parts) < 2:
            continue
        name, kind = parts[0], parts[1]
        if kind == "interface":
            current = {"name": name, "properties": [], "methods": [], "signals": []}
            interfaces.append(current)
        elif current and kind in {"property", "method", "signal"}:
            current[KIND_BUCKETS[kind]].append({"name": name.lstrip("."), "raw": line})
    return {"returncode": proc.returncode, "stderr": proc.stderr.strip(), "interfaces": interfaces}


buses = ["user", "system"] if INCLUDE_SYSTEM else ["user"]
print(json.dumps({"service": SERVICE, "paths": {bus: {path: introspect(bus, path) for path in PATHS} for bus in buses}}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def deckard_power_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
from pathlib import Path

paths = [
    "/run/deckardcharger/battery_status",
    "/run/deckardcharger/battery_percent",
    "/run/deckardcharger/ac_status",
    "/run/deckardcharger/secs_until_battery_full",
    "/run/deckardcharger/secs_until_shutdown_request",
]
values = {}
for item in paths:
    path = Path(item)
    try:
        values[item] = {"exists": path.exists(), "value": path.read_text(encoding="utf-8", errors="replace").strip()}
    except OSError as exc:
        values[item] = {"exists": path.exists(), "error": str(exc)}
sysfs = {}
for path in Path("/sys/class/power_supply").glob("*"):
    if not path.is_dir():
        continue
    props = {}
    for name in ("type", "status", "capacity", "online", "voltage_now", "current_now", "charge_now", "energy_now"):
        prop = path / name
        if prop.exists():
            try:
                props[name] = prop.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
    if props:
        sysfs[str(path)] = props
print(json.dumps({"runtime_files": values, "sysfs_power_supply": sysfs}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def pidbridge_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import os
import stat
import subprocess
from pathlib import Path

socket_path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pidbridge/pidbridge.sock"
unit = subprocess.run(
    ["systemctl", "--user", "show", "pidbridge.service", "--no-pager", "-pActiveState", "-pSubState", "-pMainPID", "-pFragmentPath", "-pExecStart"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
unit_status = {}
for line in unit.stdout.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        unit_status[key] = value
sock = {"path": str(socket_path), "exists": socket_path.exists()}
if socket_path.exists():
    st = socket_path.stat()
    sock.update({"mode": oct(stat.S_IMODE(st.st_mode)), "uid": st.st_uid, "gid": st.st_gid, "is_socket": stat.S_ISSOCK(st.st_mode)})
print(json.dumps({"unit": unit_status, "unit_stderr": unit.stderr.strip(), "socket": sock}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": ["Socket protocol is not exposed; this reports service and socket state only."],
        }

    def deckard_runtime_environment(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
from pathlib import Path

files = [
    "/usr/share/deckard/version",
    "/usr/share/deckard/mesavars.sh",
    "/usr/share/deckard/steam_launch_wrapper_env_defaults.txt",
]
entries = {}
for item in files:
    path = Path(item)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        entries[item] = {"exists": True, "text": text[:12000], "truncated": len(text) > 12000}
    except OSError as exc:
        entries[item] = {"exists": path.exists(), "error": str(exc)}
print(json.dumps({"files": entries}))
""",
        )
        return {"device": to_jsonable(device), **data}

    def steam_frame_openxr_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import os
from pathlib import Path


def inspect(path_text):
    path = Path(path_text).expanduser()
    item = {
        "path": str(path),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "resolved": None,
        "json": None,
        "text": None,
        "error": None,
    }
    try:
        if path.is_symlink():
            item["link_target"] = os.readlink(path)
        item["resolved"] = str(path.resolve(strict=False))
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            item["text"] = text[:4000]
            try:
                item["json"] = json.loads(text)
            except Exception:
                pass
    except OSError as exc:
        item["error"] = str(exc)
    return item


home = Path.home()
paths = {
    "native_user_active_runtime": home / ".config/openxr/1/active_runtime.json",
    "native_steamvr_runtime": Path("/opt/steamvr/steamxr_linuxarm64.json"),
    "lepton_overlay_active_runtime": (
        home
        / ".local/share/Steam/steamapps/common/Lepton/images/rootfs_overlay/vendor/etc/openxr/1/active_runtime.json"
    ),
    "lepton_guestos_active_runtime": Path("/usr/share/guestos/android/vendor/etc/openxr/1/active_runtime.json"),
}
print(json.dumps({"paths": {key: inspect(str(path)) for key, path in paths.items()}}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "Native SteamVR setup links ~/.config/openxr/1/active_runtime.json to the SteamVR runtime.",
                "Lepton Android uses the vendor active_runtime.json mounted into the container.",
            ],
        }

    def lepton_graphics_debug_status(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import subprocess
from pathlib import Path

home = Path.home()
lepton_root = home / ".local/share/Steam/steamapps/common/Lepton"
helpers = {
    "vulkan_layers": lepton_root / "liblepton/vulkan_layers.sh",
    "renderdoc": lepton_root / "liblepton/renderdoc.sh",
    "vulkan_validation": lepton_root / "liblepton/vulkan_validation.sh",
    "fdm_injection": lepton_root / "liblepton/fdm_injection.sh",
    "perfetto": lepton_root / "liblepton/perfetto.sh",
    "strace": lepton_root / "liblepton/strace.sh",
    "gdb": lepton_root / "liblepton/gdb.sh",
}
helper_status = {}
for key, path in helpers.items():
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        helper_status[key] = {"exists": True, "path": str(path), "size": path.stat().st_size, "preview": text[:2000]}
    except OSError as exc:
        helper_status[key] = {"exists": path.exists(), "path": str(path), "error": str(exc)}

layer_roots = [
    Path("/usr/share/guestos/android/vendor/vulkan_layers"),
    lepton_root / "vulkan_layers",
]
layers = {}
for root in layer_roots:
    entries = []
    if root.is_dir():
        for path in sorted(root.glob("*.so"))[:100]:
            try:
                entries.append({"name": path.name, "path": str(path), "size": path.stat().st_size})
            except OSError:
                entries.append({"name": path.name, "path": str(path)})
    layers[str(root)] = {"exists": root.is_dir(), "entries": entries}

special_files = {
    "perfetto_tracebox": Path("/usr/share/guestos/android/perfetto/tracebox"),
    "renderdoc_android_apk": Path("/usr/share/renderdoc/plugins/android/org.renderdoc.renderdoccmd.arm64.apk"),
}
files = {}
for key, path in special_files.items():
    try:
        files[key] = {"exists": path.exists(), "path": str(path), "size": path.stat().st_size if path.exists() else None}
    except OSError as exc:
        files[key] = {"exists": path.exists(), "path": str(path), "error": str(exc)}

mesa = subprocess.run(["mesa_version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=10)
print(
    json.dumps(
        {
            "helpers": helper_status,
            "vulkan_layers": layers,
            "files": files,
            "mesa_version": {
                "returncode": mesa.returncode,
                "stdout": mesa.stdout.strip(),
                "stderr": mesa.stderr.strip(),
            },
            "env_flags": {
                "renderdoc": ["ENABLE_VULKAN_RENDERDOC_CAPTURE", "VK_INSTANCE_LAYERS=VK_LAYER_RENDERDOC_Capture"],
                "validation": ["ENABLE_VULKAN_VALIDATION_LAYER"],
                "fdm": ["ENABLE_VULKAN_FDM_INJECTION_LAYER"],
                "rpo": ["ENABLE_VULKAN_RPO_LAYER"],
                "frame_markers": ["EnableFrameEndMarkers"],
                "timeline": ["DisableTimelineSemaphoreWait"],
                "strace": ["LEPTON_STRACE", "LEPTON_STRACE_ARGS"],
            },
        }
    )
)
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "This is a readiness/status probe; it does not enable layers or install debug packages.",
                "RenderDoc and Vulkan layer activation should be done through launch settings or gated tools.",
            ],
        }

    def lepton_artifacts_manifest(
        self,
        ref: DeviceRef,
        context: str = "dev",
        package_name: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        clean_context = self._validate_lepton_context(context)
        if package_name and not ANDROID_PACKAGE_RE.fullmatch(package_name):
            raise DevkitAdapterError(f"Invalid Android package name: {package_name}")
        bounded_limit = min(max(int(limit), 1), 500)
        data = self._remote_python_json(
            device,
            "CONTEXT = "
            + json.dumps(clean_context)
            + "\nPACKAGE_NAME = "
            + json.dumps(package_name or "")
            + "\nLIMIT = "
            + str(bounded_limit)
            + r"""
import fnmatch
import json
from pathlib import Path

home = Path.home()
lepton_root = home / ".local/share/Steam/steamapps/common/Lepton"
roots = [
    Path("/tmp"),
    lepton_root / "perfetto",
    home / ".local/share/Steam/logs",
    home / ".config/openvr/logs",
]
patterns = [
    f"lepton-{CONTEXT}*.pftrace",
    f"lepton-{CONTEXT}.log",
    f"*{CONTEXT}*perfetto*",
    f"*{CONTEXT}*bootchart*",
    f"*{CONTEXT}*strace*",
]
if PACKAGE_NAME:
    patterns += [
        f"*{PACKAGE_NAME}*",
        f"strace-{PACKAGE_NAME}.log",
        f"strace-{PACKAGE_NAME}.debug",
    ]

entries = []
for root in roots:
    if not root.exists():
        continue
    try:
        candidates = [path for path in root.rglob("*") if path.is_file()]
    except OSError:
        continue
    for path in candidates:
        rel = path.name
        full = str(path)
        if not any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(full, pattern) for pattern in patterns):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": str(path),
                "root": str(root),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
entries.sort(key=lambda item: item["mtime"], reverse=True)
print(json.dumps({"context": CONTEXT, "package_name": PACKAGE_NAME or None, "limit": LIMIT, "entries": entries[:LIMIT]}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "Lepton perfetto traces are typically /tmp/lepton-<context>-*.pftrace.",
                "Strace/debug files are only present after a launch configured with Lepton strace flags.",
            ],
        }

    def steam_frame_tracking_datasets(self, ref: DeviceRef, limit: int = 10) -> dict[str, Any]:
        device = self.resolve_device(ref)
        bounded_limit = min(max(int(limit), 1), 100)
        data = self._remote_python_json(
            device,
            "LIMIT = " + str(bounded_limit) + r"""
import json
from pathlib import Path

root = Path.home() / ".config/openvr/config/cv/xrservice/datasets"
entries = []
if root.is_dir():
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not path.is_dir():
            continue
        try:
            stat = path.stat()
            file_count = 0
            total_bytes = 0
            for child in path.rglob("*"):
                if child.is_file():
                    file_count += 1
                    total_bytes += child.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "path": str(path),
                "name": path.name,
                "mtime": stat.st_mtime,
                "file_count": file_count,
                "total_bytes": total_bytes,
            }
        )
print(json.dumps({"root": str(root), "exists": root.is_dir(), "limit": LIMIT, "datasets": entries[:LIMIT]}))
""",
        )
        return {
            "device": to_jsonable(device),
            **data,
            "notes": [
                "Tracking datasets are created from SteamVR Developer settings on the headset.",
                "Use sync_tracking_dataset to download one after recording.",
            ],
        }

    def sync_tracking_dataset(
        self,
        ref: DeviceRef,
        output_folder: str,
        dataset_path: str | None = None,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        manifest = self.steam_frame_tracking_datasets(ref, limit=100)
        datasets = manifest.get("datasets") or []
        if not datasets:
            raise DevkitAdapterError("No tracking datasets were found on the device")
        root = str(manifest.get("root") or "")
        selected = None
        if dataset_path:
            requested = dataset_path.strip().rstrip("/")
            for item in datasets:
                if item.get("path", "").rstrip("/") == requested or item.get("name") == requested:
                    selected = item
                    break
            if selected is None:
                raise DevkitAdapterError(f"Dataset path/name was not found under {root}: {dataset_path}")
        else:
            selected = datasets[0]
        remote_path = str(selected["path"])
        if not remote_path.startswith(root.rstrip("/") + "/"):
            raise DevkitAdapterError(f"Refusing to download dataset outside expected root: {remote_path}")
        transfer = self.rsync_transfer(output_folder, device, remote_path, upload=False)
        return {
            "device": to_jsonable(device),
            "dataset": selected,
            "local_folder": str(Path(output_folder).expanduser()),
            "transfer": transfer,
        }

    def steam_services(
        self,
        ref: DeviceRef,
        scope: str = "user",
        pattern: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        normalized_scope = scope.lower()
        if normalized_scope not in {"user", "system"}:
            raise DevkitAdapterError("scope must be 'user' or 'system'")
        bounded_limit = min(max(int(limit), 1), 500)
        data = self._remote_python_json(
            device,
            "SCOPE = "
            + json.dumps(normalized_scope)
            + "\nPATTERN = "
            + json.dumps(pattern or "")
            + "\nLIMIT = "
            + str(bounded_limit)
            + r"""

import json
import subprocess

args = ["systemctl"]
if SCOPE == "user":
    args.append("--user")
args += ["list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend"]
proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
services = []
for line in proc.stdout.splitlines():
    parts = line.split(None, 4)
    if len(parts) < 4:
        continue
    description = parts[4] if len(parts) > 4 else ""
    haystack = line.lower()
    if PATTERN and PATTERN.lower() not in haystack:
        continue
    services.append(
        {
            "unit": parts[0],
            "load": parts[1],
            "active": parts[2],
            "sub": parts[3],
            "description": description,
        }
    )
    if len(services) >= LIMIT:
        break

print(
    json.dumps(
        {
            "scope": SCOPE,
            "pattern": PATTERN,
            "services": services,
            "stderr": proc.stderr.strip(),
            "returncode": proc.returncode,
        }
    )
)
""",
        )
        return {"device": to_jsonable(device), **data}

    def journalctl_tail(
        self,
        ref: DeviceRef,
        unit: str,
        lines: int = 200,
        scope: str = "user",
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        normalized_scope = scope.lower()
        if normalized_scope not in {"user", "system"}:
            raise DevkitAdapterError("scope must be 'user' or 'system'")
        if not SYSTEMD_UNIT_RE.fullmatch(unit):
            raise DevkitAdapterError(f"Invalid systemd unit name: {unit}")
        bounded_lines = min(max(int(lines), 1), 2000)
        parts = ["journalctl"]
        if normalized_scope == "user":
            parts.append("--user")
        parts += ["-u", unit, "-n", str(bounded_lines), "--no-pager", "-o", "short-iso"]
        out, err, status = self.simple_ssh(
            device,
            " ".join(shlex.quote(part) for part in parts),
            silent=True,
        )
        return {
            "device": to_jsonable(device),
            "scope": normalized_scope,
            "unit": unit,
            "exit_status": status,
            "stderr": err,
            "lines": out.splitlines(),
        }

    def steam_frame_dev_inventory(self, ref: DeviceRef) -> dict[str, Any]:
        device = self.resolve_device(ref)
        data = self._remote_python_json(
            device,
            r"""
import json
import os
import re
import subprocess
from pathlib import Path


def run(args, timeout=20):
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def read_text(path, limit=20000):
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    truncated = len(data) > limit
    return {"path": path, "text": data[:limit], "truncated": truncated}


def parse_services(scope):
    args = ["systemctl"]
    if scope == "user":
        args.append("--user")
    args += ["list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend"]
    proc = run(args)
    needles = ("steam", "steamvr", "gamescope", "deckard", "lepton", "adb", "devkit", "pidbridge", "xrdp")
    services = []
    for line in proc["stdout"].splitlines():
        if not any(needle in line.lower() for needle in needles):
            continue
        parts = line.split(None, 4)
        if len(parts) >= 4:
            services.append(
                {
                    "unit": parts[0],
                    "load": parts[1],
                    "active": parts[2],
                    "sub": parts[3],
                    "description": parts[4] if len(parts) > 4 else "",
                }
            )
    return {"returncode": proc["returncode"], "stderr": proc["stderr"].strip(), "services": services}


def bus_names(scope):
    args = ["busctl"]
    if scope == "user":
        args.append("--user")
    args += ["list", "--no-pager", "--no-legend"]
    proc = run(args)
    names = []
    needles = ("steam", "gamescope", "deckard", "lepton", "vr", "pidbridge", "manager")
    for line in proc["stdout"].splitlines():
        if any(needle in line.lower() for needle in needles):
            names.append(line)
    return {"returncode": proc["returncode"], "stderr": proc["stderr"].strip(), "names": names[:120]}


def script_summary(path):
    summary = {"path": path, "exists": os.path.exists(path), "functions": [], "env_refs": []}
    if not summary["exists"]:
        return summary
    try:
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if re.match(r"^(function\s+)?[A-Za-z0-9_]+\(\)", stripped):
                summary["functions"].append({"line": lineno, "text": stripped})
            if re.search(r"\b(LEPTON_|ENABLE_|RENDERDOC_|SteamAppId|SteamGameId|ADB_|GDB_|LLDB_)", line):
                summary["env_refs"].append({"line": lineno, "text": stripped[:220]})
            if len(summary["functions"]) >= 80 and len(summary["env_refs"]) >= 80:
                break
    except OSError as exc:
        summary["error"] = str(exc)
    summary["functions"] = summary["functions"][:120]
    summary["env_refs"] = summary["env_refs"][:120]
    return summary


def binary_summary(path):
    summary = {"path": path, "exists": os.path.exists(path)}
    if not summary["exists"]:
        return summary
    summary["file"] = run(["file", path])["stdout"].strip()
    strings = run(["strings", "-a", path], timeout=30)
    pattern = re.compile(
        r"steam|steamos|lepton|deckard|adb|debug|trace|perf|capture|renderdoc|vulkan|"
        r"openxr|gdb|lldb|dbus|busctl|service|journal|podman|container",
        re.IGNORECASE,
    )
    matches = []
    for line in strings["stdout"].splitlines():
        if pattern.search(line):
            matches.append(line[:240])
        if len(matches) >= 120:
            break
    summary["strings_returncode"] = strings["returncode"]
    summary["interesting_strings"] = matches
    return summary


home = os.path.expanduser("~")
lepton_root = os.path.join(home, ".local/share/Steam/steamapps/common/Lepton")
lepton_lib = os.path.join(lepton_root, "liblepton")
script_names = [
    "liblepton.sh",
    "mounting.sh",
    "networking.sh",
    "properties.sh",
    "perfetto.sh",
    "renderdoc.sh",
    "strace.sh",
    "gdb.sh",
    "vulkan_layers.sh",
    "performance_debugging.sh",
    "utils.sh",
]
binary_paths = [
    "/usr/lib/steamos-manager",
    "/usr/bin/pidbridge",
    "/usr/bin/gamescope",
    "/usr/bin/steam",
    os.path.join(lepton_lib, "apk_extractor/bin/apk-info-extractor"),
]
tool_names = [
    "podman",
    "gamescope",
    "steam",
    "busctl",
    "journalctl",
    "coredumpctl",
    "perfetto",
    "renderdoccmd",
    "strace",
    "gdbserver",
    "lldb-server",
]

lepton_help = run([os.path.join(lepton_root, "lepton"), "help"]) if os.path.exists(os.path.join(lepton_root, "lepton")) else {"returncode": 127, "stdout": "", "stderr": "Lepton CLI not found"}
devkit_utils = []
devkit_dir = Path(home) / "devkit-utils"
if devkit_dir.exists():
    devkit_utils = sorted(path.name for path in devkit_dir.iterdir() if path.is_file())

print(
    json.dumps(
        {
            "os_release": read_text("/etc/os-release"),
            "uname": run(["uname", "-a"])["stdout"].strip(),
            "tool_paths": {name: run(["sh", "-lc", f"command -v {name} || true"])["stdout"].strip() for name in tool_names},
            "devkit_utils": devkit_utils,
            "lepton_root": lepton_root,
            "lepton_help": {
                "returncode": lepton_help["returncode"],
                "stdout": lepton_help["stdout"][:12000],
                "stderr": lepton_help["stderr"],
            },
            "lepton_scripts": [script_summary(os.path.join(lepton_lib, name)) for name in script_names],
            "services": {"user": parse_services("user"), "system": parse_services("system")},
            "dbus": {"user": bus_names("user"), "system": bus_names("system")},
            "binary_candidates": [binary_summary(path) for path in binary_paths],
            "notes": [
                "This inventory is static/read-only and intentionally bounded to known Steam Frame dev surfaces.",
                "Use binary_candidates interesting strings to decide which files deserve local IDA/Ghidra decompilation.",
            ],
        }
    )
)
""",
        )
        return {"device": to_jsonable(device), **data}

    def screenshot(
        self,
        ref: DeviceRef,
        output_folder: str,
        filename: str | None = None,
        timestamp: bool = True,
        xprop: int = 1,
        is_deckard: bool | None = None,
    ) -> dict[str, Any]:
        device = self.resolve_device(ref)
        if is_deckard is None:
            with contextlib.suppress(Exception):
                is_deckard = bool(self.get_steamos_status(ref).get("is_deckard"))
        ssh = self._open_ssh(device)
        try:
            if is_deckard:
                out, _, status = self._simple_ssh_client(
                    ssh, "python3 ~/devkit-utils/deckard-capture --json"
                )
                if status != 0:
                    raise DevkitAdapterError(out)
                result = json.loads(out)
                remote_path = result.get("output")
                if not remote_path:
                    raise DevkitAdapterError(result.get("error", "deckard-capture returned no output"))
            else:
                self._simple_ssh_client(ssh, "rm /tmp/gamescope*.png", silent=True)
                self._simple_ssh_client(
                    ssh,
                    "DISPLAY=:0 xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT "
                    f"32c -set GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT {int(xprop)}",
                    silent=True,
                    check_status=True,
                )
                remote_path = None
                for _ in range(100):
                    time.sleep(0.1)
                    out, _, status = self._simple_ssh_client(
                        ssh, 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"', silent=True
                    )
                    if status == 0 and out.strip():
                        remote_path = out.splitlines()[0]
                        break
                if remote_path is None:
                    raise DevkitAdapterError("Could not retrieve screenshot: timeout")

            local_path = self._next_local_capture_path(output_folder, remote_path, filename, timestamp)
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            ssh.open_sftp().get(remote_path, local_path)
            self._simple_ssh_client(ssh, f"rm {shlex.quote(remote_path)}", silent=True)
            return {"device": to_jsonable(device), "local_path": local_path, "remote_path": remote_path}
        finally:
            ssh.close()

    def gpu_trace(self, ref: DeviceRef, local_filename: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        ssh = self._open_ssh(device)
        try:
            remote_trace = self._ssh_checked(ssh, "mktemp -p /tmp XXXXXXXXX-trace.zip").strip()
            self._simple_ssh_client(ssh, f"rm {shlex.quote(remote_trace)}", silent=True, check_status=True)
            capture_cmd = f"gpu-trace --capture --no-gpuvis -o {shlex.quote(remote_trace)}"
            _, err, status = self._simple_ssh_client(ssh, capture_cmd)
            if status != 0 and "gpu-trace: command not found" in err:
                raise DevkitAdapterError("gpu-trace not found on device")
            if status != 0 or "Failed to capture trace" in err:
                self._simple_ssh_client(ssh, "gpu-trace --start")
                time.sleep(5)
                _, err, status = self._simple_ssh_client(ssh, capture_cmd)
            if status != 0:
                raise DevkitAdapterError(err or "gpu-trace failed")
            Path(local_filename).parent.mkdir(parents=True, exist_ok=True)
            ssh.open_sftp().get(remote_trace, local_filename)
            self._simple_ssh_client(ssh, f"rm {shlex.quote(remote_trace)}", silent=True)
            return {"device": to_jsonable(device), "local_path": local_filename}
        finally:
            ssh.close()

    def rgp_capture(self, ref: DeviceRef, output_folder: str) -> dict[str, Any]:
        device = self.resolve_device(ref)
        ssh = self._open_ssh(device)
        try:
            self._simple_ssh_client(ssh, "rm /tmp/*.rgp", silent=True)
            self._simple_ssh_client(ssh, "touch /tmp/rgp.trigger", silent=True)
            remote_path = None
            for _ in range(20):
                out, _, status = self._simple_ssh_client(ssh, "ls -1t /tmp/*.rgp", silent=True)
                if status == 0 and out.strip():
                    remote_path = out.splitlines()[0]
                    break
                time.sleep(0.1)
            if remote_path is None:
                raise DevkitAdapterError(
                    "Timeout waiting for RGP capture. Check graphics profiling is enabled."
                )
            size = -1
            while True:
                out, _, _ = self._simple_ssh_client(ssh, f"stat -c %s {shlex.quote(remote_path)}", silent=True, check_status=True)
                new_size = int(out)
                if new_size != 0 and new_size == size:
                    break
                size = new_size
                time.sleep(0.5)
            local_path = str(Path(output_folder) / PurePosixPath(remote_path).name)
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            ssh.open_sftp().get(remote_path, local_path)
            return {"device": to_jsonable(device), "local_path": local_path}
        finally:
            ssh.close()

    def set_renderdoc_replay(self, ref: DeviceRef, enabled: bool) -> dict[str, Any]:
        device = self.resolve_device(ref)
        self.simple_ssh(device, "killall -9 renderdoccmd", silent=True, check_status=False)
        if enabled:
            if not device.login:
                raise DevkitAdapterError("Device login is required")
            self.simple_ssh(
                device,
                f"RENDERDOC_TEMP=/home/{shlex.quote(device.login)} renderdoccmd remoteserver -d",
                silent=True,
                check_status=True,
            )
        return {"device": to_jsonable(device), "enabled": enabled}

    def adb_devices(self) -> dict[str, Any]:
        result = self._run_adb(["devices", "-l"], timeout=15)
        return {**result, "devices": _parse_adb_devices(result["stdout"])}

    def adb_connect_lepton_wifi(self, host: str = "frame", port: int = 5555) -> dict[str, Any]:
        target = host if ":" in host else f"{host}:{port}"
        return self._run_adb(["connect", target], timeout=20)

    def adb_connect_lepton_usb(self, local_port: int = 5555, remote_port: int = 5555) -> dict[str, Any]:
        forward = self.adb_forward(local_port, remote_port)
        connect = self._run_adb(["connect", f"localhost:{local_port}"], timeout=20)
        return {"forward": forward, "connect": connect}

    def adb_disconnect(self, target: str | None = None) -> dict[str, Any]:
        args = ["disconnect"]
        if target:
            args.append(target)
        return self._run_adb(args, timeout=15)

    def adb_forward(self, local_port: int, remote_port: int, serial: str | None = None) -> dict[str, Any]:
        return self._run_adb(
            self._adb_serial_args(serial)
            + ["forward", f"tcp:{int(local_port)}", f"tcp:{int(remote_port)}"],
            timeout=15,
        )

    def adb_reverse(self, device_port: int, host_port: int, serial: str | None = None) -> dict[str, Any]:
        return self._run_adb(
            self._adb_serial_args(serial)
            + ["reverse", f"tcp:{int(device_port)}", f"tcp:{int(host_port)}"],
            timeout=15,
        )

    def adb_install_apk(
        self,
        apk_path: str,
        serial: str | None = None,
        replace: bool = True,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        apk = Path(apk_path).expanduser()
        if not apk.is_file():
            raise DevkitAdapterError(f"APK not found: {apk}")
        args = self._adb_serial_args(serial) + ["install"]
        if replace:
            args.append("-r")
        args += extra_args or []
        args.append(str(apk))
        return self._run_adb(args, timeout=180)

    def adb_shell(self, command: str, serial: str | None = None, timeout: int = 60) -> dict[str, Any]:
        if not command.strip():
            raise DevkitAdapterError("adb_shell requires a non-interactive command")
        return self._run_adb(self._adb_serial_args(serial) + ["shell", command], timeout=timeout)

    def adb_logcat(
        self,
        serial: str | None = None,
        lines: int = 200,
        filter_args: list[str] | None = None,
        clear_first: bool = False,
    ) -> dict[str, Any]:
        if filter_args and not clear_first:
            unsafe = [arg for arg in filter_args if arg.startswith("-")]
            if unsafe:
                raise DevkitAdapterError(
                    "Read-only adb_logcat only accepts filterspec tokens. "
                    f"Use clear_first=True with confirmation for logcat options: {unsafe}"
                )
        if clear_first:
            self._run_adb(self._adb_serial_args(serial) + ["logcat", "-c"], timeout=20)
        args = self._adb_serial_args(serial) + ["logcat", "-d", "-t", str(max(1, int(lines)))]
        args += filter_args or []
        return self._run_adb(args, timeout=60)

    def adb_bugreport(self, output_path: str, serial: str | None = None) -> dict[str, Any]:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        args = self._adb_serial_args(serial) + ["bugreport", str(path)]
        return self._run_adb(args, timeout=300)

    def adb_unreal_insights_setup(
        self,
        tracehost: str = "127.0.0.1",
        port: int = 1981,
        serial: str | None = None,
    ) -> dict[str, Any]:
        shell = self.adb_shell(f"setprop debug.ue.commandline -tracehost={tracehost}", serial=serial)
        reverse = self.adb_reverse(device_port=port, host_port=port, serial=serial)
        return {"shell": shell, "reverse": reverse}

    def adb_lepton_app_diagnostics(
        self,
        package_name: str,
        serial: str | None = None,
        log_lines: int = 400,
    ) -> dict[str, Any]:
        package = package_name.strip()
        if not ANDROID_PACKAGE_RE.fullmatch(package):
            raise DevkitAdapterError(f"Invalid Android package name: {package_name}")
        quoted_package = shlex.quote(package)
        shell_script = (
            f"pkg={quoted_package}; "
            "pid=\"$(pidof \"$pkg\" 2>/dev/null || true)\"; "
            "echo __PID__; echo \"$pid\"; "
            "echo __ACTIVITY__; "
            "dumpsys activity activities | grep -E "
            "'mResumedActivity|ResumedActivity|topResumed|com\\.unity3d|UnityPlayerActivity|'\"$pkg\" "
            "| head -80 || true; "
            "echo __OBB_LINK__; ls -ld \"/sdcard/Android/obb/$pkg\" 2>&1 || true; "
            "echo __OBB_TARGET__; ls -lL \"/sdcard/Android/obb/$pkg\" 2>&1 || true; "
            "echo __STEAM_ENV__; "
            "if [ -n \"$pid\" ]; then "
            "tr '\\0' '\\n' < \"/proc/$pid/environ\" | grep -E 'Steam(App|Game)Id' || true; "
            "fi"
        )
        state = self._run_adb(self._adb_serial_args(serial) + ["shell", shell_script], timeout=40)
        logcat = self._run_adb(
            self._adb_serial_args(serial) + ["logcat", "-d", "-t", str(max(1, int(log_lines)))],
            timeout=60,
        )
        return {
            "package_name": package,
            "state": state,
            "logcat": {
                "adb_path": logcat["adb_path"],
                "args": logcat["args"],
                "returncode": logcat["returncode"],
                "stderr": logcat["stderr"],
                "highlight_lines": _matching_lines(logcat["stdout"], ADB_LOG_HIGHLIGHT_RE, limit=250),
            },
            "notes": [
                "For Unity split APKs, verify /sdcard/Android/obb/<package> contains "
                "main.<versionCode>.<package>.obb.",
                "Healthy Steam Frame VR launches usually reach XR_SESSION_STATE_SYNCHRONIZED "
                "and SteamAPI_Init result OK.",
            ],
        }

    def simple_ssh(
        self,
        device: DeviceInfo,
        command: str,
        silent: bool = False,
        check_status: bool = False,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        ssh = self._open_ssh(device)
        try:
            return self._simple_ssh_client(
                ssh,
                command,
                silent=silent,
                check_status=check_status,
                timeout=timeout,
            )
        finally:
            ssh.close()

    def rsync_transfer(
        self,
        localdir: str,
        device: DeviceInfo,
        remotedir: str | list[str],
        upload: bool,
        remote_user: str | None = None,
        delete_extraneous: bool = False,
        skip_newer_files: bool = False,
        verify_checksums: bool = False,
        extra_cmdline: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        local_path = Path(localdir).expanduser()
        if upload and not local_path.is_dir():
            raise DevkitAdapterError(f"Source directory does not exist: {local_path}")
        local_path.mkdir(parents=True, exist_ok=True)
        user = remote_user or device.login
        if not user:
            raise DevkitAdapterError("Device login is required for rsync")
        rsync = self._tool("rsync")
        ssh = self._tool("ssh")
        local_arg = str(local_path)
        if platform.system() == "Windows":
            local_arg = self._native_to_cygwin_path(local_arg)
        ssh_known_hosts = self._ssh_known_hosts_arg()
        rsh_cmd = f"{shlex.quote(ssh)} {ssh_known_hosts} -o StrictHostKeyChecking=no -i {shlex.quote(str(self.layout.ssh_key_path))}"
        cmd = [
            rsync,
            "-av",
            "--chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx",
            "-e",
            rsh_cmd,
        ]
        if dry_run:
            cmd.append("--dry-run")
        if delete_extraneous:
            cmd += ["--delete", "--delete-excluded", "--delete-delay"]
        if skip_newer_files:
            cmd.append("--update")
        if verify_checksums:
            cmd.append("--checksum")
        cmd += extra_cmdline or []
        if isinstance(remotedir, list):
            if upload:
                raise DevkitAdapterError("Multiple remote sources are only valid for downloads")
            cmd += [f"{user}@{device.address}:{source.rstrip('/')}" for source in remotedir]
            cmd.append(f"{local_arg.rstrip('/')}/")
        else:
            transfer_pair = [
                f"{local_arg.rstrip('/')}/",
                f"{user}@{device.address}:{remotedir.rstrip('/')}/",
            ]
            if not upload:
                transfer_pair.reverse()
            cmd += transfer_pair
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
        ret = proc.wait()
        if ret != 0:
            raise DevkitAdapterError(f"rsync failed with exit {ret}: {'; '.join(lines[-10:])}")
        return {"command": _redact_command(cmd), "returncode": ret, "output": lines[-200:]}

    def _open_ssh(self, device: DeviceInfo) -> paramiko.SSHClient:
        if not device.address:
            raise DevkitAdapterError("Device address is required")
        if not device.login:
            raise DevkitAdapterError("Device login is required")
        key_info = self.ensure_ssh_key()
        key = paramiko.RSAKey.from_private_key_file(key_info["key_path"])
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                device.address,
                username=device.login,
                pkey=key,
                timeout=REQUEST_TIMEOUT,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException:
            password = os.environ.get("STEAMOS_DEVKIT_SSH_PASSWORD") or os.environ.get(
                "STEAMOS_DEVKIT_PASSWORD"
            )
            if not password:
                raise
            ssh.close()
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                device.address,
                username=device.login,
                password=password,
                timeout=REQUEST_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
        return ssh

    def _run_adb(self, args: list[str], timeout: int) -> dict[str, Any]:
        adb = self.layout.locate_adb()
        if not adb:
            raise DevkitAdapterError("ADB was not found. Install Android SDK Platform Tools or set ADB_PATH.")
        proc = subprocess.run(
            [adb] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            check=False,
        )
        return {
            "adb_path": adb,
            "args": args,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @staticmethod
    def _adb_serial_args(serial: str | None) -> list[str]:
        return ["-s", serial] if serial else []

    @staticmethod
    def _validate_lepton_context(context: str) -> str:
        clean_context = context.strip()
        if not LEPTON_CONTEXT_RE.fullmatch(clean_context):
            raise DevkitAdapterError(f"Invalid Lepton context name: {context}")
        return clean_context

    @staticmethod
    def _select_top_level_apk(root: Path, apk_name: str | None = None) -> Path:
        if apk_name:
            apk = root / apk_name
            if not apk.is_file():
                raise DevkitAdapterError(f"APK not found under {root}: {apk_name}")
            return apk
        apks = sorted(root.glob("*.apk"))
        if not apks:
            raise DevkitAdapterError(f"No top-level APK found in {root}")
        if len(apks) > 1:
            names = ", ".join(path.name for path in apks)
            raise DevkitAdapterError(f"Multiple top-level APKs found; pass apk_name. Found: {names}")
        return apks[0]

    @staticmethod
    def _simple_ssh_client(
        ssh: paramiko.SSHClient,
        command: str,
        silent: bool = False,
        check_status: bool = False,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        del silent
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        try:
            status = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
        except TimeoutError as exc:
            raise DevkitAdapterError(f"SSH command timed out after {timeout} seconds: {command}") from exc
        except socket.timeout as exc:
            raise DevkitAdapterError(f"SSH command timed out after {timeout} seconds: {command}") from exc
        if check_status and status != 0:
            raise DevkitAdapterError(err or out or f"command failed: {command}")
        return out, err, status

    def _ssh_checked(self, ssh: paramiko.SSHClient, command: str) -> str:
        out, _, _ = self._simple_ssh_client(ssh, command, silent=True, check_status=True)
        return out

    def _remote_python_json(
        self,
        device: DeviceInfo,
        script: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        command = f"timeout {int(timeout_seconds)}s python3 - <<'PY'\n" + script.strip() + "\nPY"
        out, err, status = self.simple_ssh(
            device,
            command,
            silent=True,
            timeout=timeout_seconds + 10,
        )
        if status != 0:
            raise DevkitAdapterError(err or out or "remote python command failed")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DevkitAdapterError(f"Could not parse remote JSON output: {out!r}") from exc
        if not isinstance(data, dict):
            raise DevkitAdapterError(f"Remote JSON output was not an object: {out!r}")
        return data

    def _discover_mdns(self, timeout_seconds: float) -> list[DeviceInfo]:
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        except ImportError as exc:
            raise ImportError("zeroconf is required for mDNS discovery") from exc

        class Listener(ServiceListener):  # type: ignore[misc, valid-type]
            def __init__(self) -> None:
                self.devices: list[DeviceInfo] = []

            def add_service(self, zc: Any, service_type: str, name: str) -> None:
                self._record(zc, service_type, name)

            def update_service(self, zc: Any, service_type: str, name: str) -> None:
                self._record(zc, service_type, name)

            def remove_service(self, zc: Any, service_type: str, name: str) -> None:
                del zc, service_type, name

            def _record(self, zc: Any, service_type: str, fqdn: str) -> None:
                info = zc.get_service_info(service_type, fqdn, timeout=5000)
                if not info or not info.addresses:
                    return
                address = socket.inet_ntoa(info.addresses[0])
                service_name = fqdn.removesuffix("." + service_type).rstrip(".")
                props = {
                    k.decode("utf-8", "replace"): v.decode("utf-8", "replace")
                    for k, v in (info.properties or {}).items()
                }
                login = props.get("login")
                device = DeviceInfo(
                    id=SteamOSDevkitAdapter._device_id(service_name, address, info.port),
                    name=service_name,
                    address=address,
                    login=login,
                    http_port=info.port or DEFAULT_HTTP_PORT,
                    service_name=service_name,
                    properties=props,
                    source="mdns",
                    last_seen=SteamOSDevkitAdapter._now(),
                )
                self.devices = [d for d in self.devices if d.id != device.id] + [device]

        zc = Zeroconf()
        listener = Listener()
        browser = ServiceBrowser(zc, STEAMOS_DEVKIT_SERVICE, listener)
        try:
            time.sleep(max(timeout_seconds, 0))
            del browser
            return listener.devices
        finally:
            zc.close()

    def _find_service_by_name(self, name: str, http_port: int) -> DeviceInfo | None:
        try:
            for device in self._discover_mdns(1.5):
                if device.name == name or device.service_name == name:
                    return device
        except Exception:
            return None
        return None if "." in name else DeviceInfo(
            id=self._device_id(name, name, http_port),
            name=name,
            address=name,
            http_port=http_port,
            service_name=name,
            source="service_name_unverified",
            last_seen=self._now(),
        )

    def _public_key_text(self, key: paramiko.PKey) -> str:
        return f"ssh-rsa {key.get_base64()} devkit-client:{getpass.getuser()}@{socket.gethostname()}\n"

    def _fix_key_permissions(self) -> None:
        key_path = self.layout.ssh_key_path
        pubkey_path = self.layout.ssh_pubkey_path
        if platform.system() != "Windows":
            with contextlib.suppress(OSError):
                key_path.chmod(0o400)
            with contextlib.suppress(OSError):
                pubkey_path.chmod(0o400)
            return
        username = _windows_domain_user()
        for cmd in (
            ["icacls.exe", str(key_path), "/Reset"],
            ["icacls.exe", str(key_path), "/Inheritance:r"],
            ["icacls.exe", str(key_path), "/Grant:r", f"{username}:(R)"],
        ):
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )

    def _shortcut_payload(self, profile: UploadProfile, destdir: str) -> dict[str, Any]:
        settings = dict(profile.settings)
        settings.update(self._runtime_settings(profile))
        return {
            "gameid": profile.gameid,
            "directory": destdir,
            "argv": profile.argv,
            "env": profile.env,
            "settings": settings,
        }

    def _runtime_settings(self, profile: UploadProfile) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        runtime = profile.runtime.lower() if profile.runtime else None
        debug_mode = DEBUG_MODE.get(profile.steam_play_debug.lower(), 0)
        if runtime:
            alias = RUNTIME_ALIASES.get(runtime, profile.runtime)
            if alias in {"proton-stable", "proton-experimental"}:
                settings["steam_play"] = "1"
                settings["steam_play_debug"] = str(debug_mode)
                settings["steam_play_debug_version"] = str(
                    profile.settings.get("steam_play_debug_version", "2022")
                )
            else:
                settings["steam_play"] = "0"
            settings["compat_tool"] = alias
        if profile.gdbserver:
            settings["gdbserver"] = "1"
        return settings

    def _write_sideload_settings(self, device: DeviceInfo, payload: dict[str, Any]) -> None:
        ssh = self._open_ssh(device)
        try:
            remote_root = f"/home/{device.login}/{DEVKIT_TOOL_FOLDER}"
            sftp = ssh.open_sftp()
            for suffix, value in (
                ("argv", payload.get("argv")),
                ("settings", payload.get("settings")),
                ("env", payload.get("env")),
            ):
                if value in (None, {}, []):
                    continue
                with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
                    json.dump(value, tmp)
                    local = tmp.name
                try:
                    sftp.put(local, str(PurePosixPath(remote_root, f"{payload['gameid']}-{suffix}.json")))
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(local)
        finally:
            ssh.close()

    def _tool(self, name: str) -> str:
        candidates = [name]
        if platform.system() == "Windows" and not name.endswith(".exe"):
            candidates.insert(0, name + ".exe")
        for candidate in candidates:
            path = self.layout.locate_tool(candidate)
            if path:
                return path
        raise DevkitAdapterError(f"Required external tool not found: {name}")

    def _native_to_cygwin_path(self, path: str) -> str:
        cygpath = self._tool("cygpath")
        proc = subprocess.run(
            [cygpath, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        if proc.returncode != 0:
            raise DevkitAdapterError(f"cygpath failed: {proc.stderr}")
        return proc.stdout.splitlines()[0]

    def _ssh_known_hosts_arg(self) -> str:
        if platform.system() != "Windows":
            return "-o UserKnownHostsFile=/dev/null"
        userprofile = os.environ.get("USERPROFILE")
        if not userprofile:
            return "-o UserKnownHostsFile=/dev/null"
        dotssh = Path(userprofile) / ".ssh"
        dotssh.mkdir(parents=True, exist_ok=True)
        return f"-o UserKnownHostsFile={shlex.quote(shlex.quote(str(dotssh / 'known_hosts')))}"

    @staticmethod
    def _device_id(name: str | None, address: str | None, http_port: int) -> str:
        payload = f"{name or ''}|{address or ''}|{http_port}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _now() -> str:
        return _dt.datetime.now(_dt.UTC).isoformat()

    @staticmethod
    def _next_local_capture_path(
        output_folder: str,
        remote_path: str,
        filename: str | None,
        timestamp: bool,
    ) -> str:
        remote_name = PurePosixPath(remote_path).name
        suffix = Path(remote_name).suffix
        base_name = Path(filename or remote_name).with_suffix("").name
        if timestamp:
            match = re.search(r"_[0-9-_]*", remote_name)
            stamp = match.group(0) if match else "-" + _dt.datetime.now().strftime("%Y%m%d%H%M%S")
            base_name += stamp
        candidate = Path(output_folder) / f"{base_name}{suffix}"
        if not candidate.exists():
            return str(candidate)
        stem = candidate.with_suffix("")
        for index in range(1000):
            next_candidate = Path(f"{stem}_{index:03}{suffix}")
            if not next_candidate.exists():
                return str(next_candidate)
        raise DevkitAdapterError(f"Could not allocate output filename under {output_folder}")


def _normalize_steam_client_mode(mode: str) -> str:
    lowered = mode.lower()
    if lowered in {"default", "os", "steamstatus.os"}:
        return "SteamStatus.OS"
    if lowered in {"os_dev", "os-dev", "dev", "steamstatus.os_dev"}:
        return "SteamStatus.OS_DEV"
    if lowered in SIDELOAD_GAMEIDS or lowered in {"sideloaded", "steamstatus.sideloaded"}:
        return "SteamStatus.SIDELOADED"
    if mode in {"SteamStatus.OS", "SteamStatus.OS_DEV", "SteamStatus.SIDELOADED"}:
        return mode
    raise DevkitAdapterError(f"Unsupported Steam client mode: {mode}")


def _windows_domain_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    username = os.environ.get("USERNAME") or os.getlogin()
    return f"{domain}\\{username}" if domain else username


def _redact_command(cmd: list[str]) -> list[str]:
    redacted = []
    skip_next = False
    for part in cmd:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(part)
        if part == "-i":
            skip_next = True
    return redacted


def _parse_adb_devices(stdout: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        details: dict[str, str] = {}
        for item in parts[2:]:
            if ":" in item:
                key, value = item.split(":", 1)
                details[key] = value
        devices.append({"serial": serial, "state": state, "details": details})
    return devices


def _matching_lines(text: str, pattern: re.Pattern[str], limit: int = 250) -> list[str]:
    lines = [line for line in text.splitlines() if pattern.search(line)]
    return lines[-limit:]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
