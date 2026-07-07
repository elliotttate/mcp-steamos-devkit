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
    ) -> tuple[str, str, int]:
        ssh = self._open_ssh(device)
        try:
            return self._simple_ssh_client(ssh, command, silent=silent, check_status=check_status)
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
        ssh.connect(
            device.address,
            username=device.login,
            pkey=key,
            timeout=REQUEST_TIMEOUT,
            look_for_keys=False,
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
    ) -> tuple[str, str, int]:
        del silent
        _, stdout, stderr = ssh.exec_command(command)
        status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if check_status and status != 0:
            raise DevkitAdapterError(err or out or f"command failed: {command}")
        return out, err, status

    def _ssh_checked(self, ssh: paramiko.SSHClient, command: str) -> str:
        out, _, _ = self._simple_ssh_client(ssh, command, silent=True, check_status=True)
        return out

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
