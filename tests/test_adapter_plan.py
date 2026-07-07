from __future__ import annotations

from pathlib import Path

from typing import Any

import pytest

from mcp_steamos_devkit.adapter import DevkitAdapterError, SteamOSDevkitAdapter, _parse_adb_devices
from mcp_steamos_devkit.config import DevkitLayout
from mcp_steamos_devkit.models import DeviceInfo, DeviceRef, UploadProfile
from mcp_steamos_devkit.state import JsonStore


def make_adapter(tmp_path: Path) -> SteamOSDevkitAdapter:
    layout = DevkitLayout(client_root=None, source_root=None, data_dir=tmp_path / "data", config_dir=tmp_path / "config")
    return SteamOSDevkitAdapter(layout=layout, store=JsonStore(layout.data_dir))


def test_validate_upload_plan_counts_files_and_flags_destructive(tmp_path: Path) -> None:
    source = tmp_path / "game"
    source.mkdir()
    (source / "a.txt").write_text("abc", encoding="utf-8")
    (source / "b.bin").write_bytes(b"12345")

    adapter = make_adapter(tmp_path)
    plan = adapter.validate_upload_plan(
        UploadProfile(
            device=DeviceRef("deck"),
            gameid="sample",
            local_dir=str(source),
            delete_extraneous=True,
            filter_args=["--exclude=*.pdb"],
        )
    )

    assert plan.exists
    assert plan.file_count == 2
    assert plan.total_bytes == 8
    assert plan.destructive
    assert len(plan.warnings) == 2


def test_runtime_settings_match_valve_keys(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    profile = UploadProfile(
        device=DeviceRef("deck"),
        gameid="sample",
        local_dir=str(tmp_path),
        runtime="proton-experimental",
        steam_play_debug="wait",
        settings={"steam_play_debug_version": "2022"},
        gdbserver=True,
    )

    settings = adapter._runtime_settings(profile)

    assert settings["steam_play"] == "1"
    assert settings["steam_play_debug"] == "2"
    assert settings["steam_play_debug_version"] == "2022"
    assert settings["compat_tool"] == "proton-experimental"
    assert settings["gdbserver"] == "1"


def test_parse_adb_devices_with_details() -> None:
    devices = _parse_adb_devices(
        "List of devices attached\n"
        "frame:5555\tdevice product:lepton model:Steam_Frame device:lepton transport_id:1\n"
        "emulator-5554\tunauthorized\n"
    )

    assert devices == [
        {
            "serial": "frame:5555",
            "state": "device",
            "details": {"product": "lepton", "model": "Steam_Frame", "device": "lepton", "transport_id": "1"},
        },
        {"serial": "emulator-5554", "state": "unauthorized", "details": {}},
    ]


def test_adb_helpers_build_documented_lepton_commands(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    calls: list[tuple[list[str], int]] = []

    def fake_run_adb(args: list[str], timeout: int) -> dict[str, Any]:
        calls.append((args, timeout))
        return {"args": args, "timeout": timeout, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(adapter, "_run_adb", fake_run_adb)

    adapter.adb_connect_lepton_wifi()
    assert calls[-1] == (["connect", "frame:5555"], 20)

    adapter.adb_connect_lepton_usb()
    assert calls[-2] == (["forward", "tcp:5555", "tcp:5555"], 15)
    assert calls[-1] == (["connect", "localhost:5555"], 20)

    adapter.adb_unreal_insights_setup(serial="frame:5555")
    assert calls[-2] == (
        ["-s", "frame:5555", "shell", "setprop debug.ue.commandline -tracehost=127.0.0.1"],
        60,
    )
    assert calls[-1] == (["-s", "frame:5555", "reverse", "tcp:1981", "tcp:1981"], 15)


def test_adb_install_and_logcat_commands(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    calls: list[tuple[list[str], int]] = []

    def fake_run_adb(args: list[str], timeout: int) -> dict[str, Any]:
        calls.append((args, timeout))
        return {"args": args, "timeout": timeout, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(adapter, "_run_adb", fake_run_adb)

    apk = tmp_path / "game.apk"
    apk.write_bytes(b"fake apk")
    adapter.adb_install_apk(str(apk), serial="frame:5555", extra_args=["-d"])
    assert calls[-1] == (["-s", "frame:5555", "install", "-r", "-d", str(apk)], 180)

    adapter.adb_logcat(serial="frame:5555", lines=50, filter_args=["UE", "*:S"], clear_first=True)
    assert calls[-2] == (["-s", "frame:5555", "logcat", "-c"], 20)
    assert calls[-1] == (["-s", "frame:5555", "logcat", "-d", "-t", "50", "UE", "*:S"], 60)

    with pytest.raises(DevkitAdapterError):
        adapter.adb_logcat(serial="frame:5555", filter_args=["-c"], clear_first=False)


def test_validate_android_split_package_detects_unity_export_obb(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "game"
    source.mkdir()
    apk = source / "vracer_frame.arm64-v8a.apk"
    apk.write_bytes(b"fake apk")
    (source / "vracer_frame.arm64-v8a.main.obb").write_bytes(b"fake obb")
    adapter = make_adapter(tmp_path)

    monkeypatch.setattr(
        adapter,
        "inspect_android_apk",
        lambda apk_path: {
            "apk_path": apk_path,
            "aapt_path": "aapt",
            "package_name": "com.judiva.vracer",
            "version_code": "1",
            "version_name": "0.1.0",
            "expected_main_obb": "main.1.com.judiva.vracer.obb",
            "badging": "",
        },
    )

    result = adapter.validate_android_split_package(str(source))

    assert not result["ok"]
    assert result["expected_obb_name"] == "main.1.com.judiva.vracer.obb"
    assert "vracer_frame.arm64-v8a.main.obb" in result["warnings"][0]
    assert result["errors"] == [f"Expected OBB is missing: {source / 'obb' / 'main.1.com.judiva.vracer.obb'}"]


def test_stage_android_obb_layout_copies_expected_name(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "game"
    source.mkdir()
    apk = source / "game.apk"
    apk.write_bytes(b"fake apk")
    obb = source / "game.main.obb"
    obb.write_bytes(b"fake obb")
    adapter = make_adapter(tmp_path)

    monkeypatch.setattr(
        adapter,
        "inspect_android_apk",
        lambda apk_path: {
            "apk_path": apk_path,
            "aapt_path": "aapt",
            "package_name": "com.example.game",
            "version_code": "42",
            "version_name": "1.0",
            "expected_main_obb": "main.42.com.example.game.obb",
            "badging": "",
        },
    )

    result = adapter.stage_android_obb_layout(str(source))
    staged = source / "obb" / "main.42.com.example.game.obb"

    assert result["copied"]
    assert staged.read_bytes() == b"fake obb"
    assert result["validation"]["ok"]

    second = adapter.stage_android_obb_layout(str(source))
    assert not second["copied"]


def test_adb_lepton_app_diagnostics_builds_fixed_commands(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    calls: list[tuple[list[str], int]] = []

    def fake_run_adb(args: list[str], timeout: int) -> dict[str, Any]:
        calls.append((args, timeout))
        stdout = ""
        if "logcat" in args:
            stdout = (
                "I/Unity: OpenXRSession::HandleSessionStateChangedEvent: state "
                "XR_SESSION_STATE_READY->XR_SESSION_STATE_SYNCHRONIZED\n"
                "I/Unity: Result: k_ESteamAPIInitResult_OK, msg=\n"
            )
        return {"adb_path": "adb", "args": args, "timeout": timeout, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(adapter, "_run_adb", fake_run_adb)

    result = adapter.adb_lepton_app_diagnostics("com.judiva.vracer", serial="frame:5556", log_lines=25)

    assert calls[0][0][:3] == ["-s", "frame:5556", "shell"]
    assert "/sdcard/Android/obb/$pkg" in calls[0][0][-1]
    assert calls[1] == (["-s", "frame:5556", "logcat", "-d", "-t", "25"], 60)
    assert result["logcat"]["highlight_lines"] == [
        "I/Unity: OpenXRSession::HandleSessionStateChangedEvent: state "
        "XR_SESSION_STATE_READY->XR_SESSION_STATE_SYNCHRONIZED",
        "I/Unity: Result: k_ESteamAPIInitResult_OK, msg=",
    ]


def test_lepton_containers_adds_adb_targets(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="192.0.2.10", login="steamos")

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)
    monkeypatch.setattr(
        adapter,
        "_remote_python_json",
        lambda resolved, script: {
            "containers": [
                {
                    "name": "lepton-steamlaunch-3570175983",
                    "context": "steamlaunch-3570175983",
                    "ports": {"adb": "5556", "gdb": "1338", "lldb": "2338"},
                    "labels": {"adb_port": "5556"},
                }
            ]
        },
    )

    result = adapter.lepton_containers(DeviceRef("frame"))

    assert result["device"]["address"] == "192.0.2.10"
    assert result["containers"][0]["adb_target"] == "192.0.2.10:5556"


def test_lepton_logcat_bounds_and_validates_context(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    calls: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_simple_ssh(
        resolved: DeviceInfo,
        command: str,
        silent: bool = False,
        check_status: bool = False,
    ) -> tuple[str, str, int]:
        del resolved, silent, check_status
        calls.append(command)
        return "I/Unity: SteamAPI_Init OK\nE/Unity: XR_ERROR_RUNTIME_FAILURE\n", "", 0

    monkeypatch.setattr(adapter, "simple_ssh", fake_simple_ssh)

    result = adapter.lepton_logcat(DeviceRef("frame"), "steamlaunch-3570175983", lines=99999)

    assert "logcat steamlaunch-3570175983" in calls[0]
    assert "tail -n 5000" in calls[0]
    assert result["highlight_lines"] == [
        "I/Unity: SteamAPI_Init OK",
        "E/Unity: XR_ERROR_RUNTIME_FAILURE",
    ]

    with pytest.raises(DevkitAdapterError):
        adapter.lepton_logcat(DeviceRef("frame"), "bad;context")


def test_steam_logs_manifest_bounds_limit_and_passes_pattern(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        scripts.append(script)
        return {"entries": [], "roots": ["/home/steamos/.local/share/Steam/logs"], "limit": 500}

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    result = adapter.steam_logs_manifest(DeviceRef("frame"), pattern="xrclient", limit=999)

    assert result["limit"] == 500
    assert 'PATTERN = "xrclient"' in scripts[0]
    assert "LIMIT = 500" in scripts[0]


def test_steam_frame_perfcriteria_parses_remote_json(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)
    monkeypatch.setattr(
        adapter,
        "_remote_python_json",
        lambda resolved, script: {
            "latest": {"path": "/home/steamos/.local/share/Steam/logs/perfcriteria.txt"},
            "parsed": {"summary": ["app line", "target line", "frame time line"]},
            "lines": ["app line", "target line", "frame time line"],
            "files": [],
        },
    )

    result = adapter.steam_frame_perfcriteria(DeviceRef("frame"))

    assert result["latest"]["path"].endswith("perfcriteria.txt")
    assert "72 fps" in result["notes"][1]


def test_journalctl_tail_quotes_valid_unit_and_rejects_invalid(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    calls: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_simple_ssh(
        resolved: DeviceInfo,
        command: str,
        silent: bool = False,
        check_status: bool = False,
    ) -> tuple[str, str, int]:
        del resolved, silent, check_status
        calls.append(command)
        return "line one\nline two\n", "", 0

    monkeypatch.setattr(adapter, "simple_ssh", fake_simple_ssh)

    result = adapter.journalctl_tail(DeviceRef("frame"), "steamvr.service", lines=5)

    assert calls == ["journalctl --user -u steamvr.service -n 5 --no-pager -o short-iso"]
    assert result["lines"] == ["line one", "line two"]

    with pytest.raises(DevkitAdapterError):
        adapter.journalctl_tail(DeviceRef("frame"), "steamvr.service; rm -rf /")


def test_steam_frame_dev_inventory_is_read_only_bounded_script(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        del resolved
        scripts.append(script)
        return {"lepton_help": {"stdout": "Usage: lepton verb"}, "binary_candidates": []}

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    result = adapter.steam_frame_dev_inventory(DeviceRef("frame"))

    assert result["device"]["name"] == "frame"
    assert result["lepton_help"]["stdout"] == "Usage: lepton verb"
    assert "binary_summary" in scripts[0]
    assert "systemctl" in scripts[0]
    assert "busctl" in scripts[0]


def test_lepton_context_inspect_adds_debug_targets_and_validates_context(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="192.0.2.10", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        del resolved
        scripts.append(script)
        return {
            "context": "dev",
            "exists": True,
            "ports": {"adb": "5555", "gdb": "1337", "lldb": "2337"},
            "mounts": [],
        }

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    result = adapter.lepton_context_inspect(DeviceRef("frame"), "dev", include_mounts=True)

    assert result["adb_target"] == "192.0.2.10:5555"
    assert result["gdb_target"] == "192.0.2.10:1337"
    assert result["lldb_target"] == "192.0.2.10:2337"
    assert 'CONTEXT = "dev"' in scripts[0]
    assert "INCLUDE_MOUNTS = True" in scripts[0]

    with pytest.raises(DevkitAdapterError):
        adapter.lepton_context_inspect(DeviceRef("frame"), "bad;context")


def test_lepton_mounts_and_debug_plan_validate_supported_values(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        del resolved
        scripts.append(script)
        return {"context": "dev", "exists": True, "category": "obb", "mounts": []}

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    mounts = adapter.lepton_mounts(DeviceRef("frame"), "dev", "obb")
    plan = adapter.lepton_debug_plan(DeviceRef("frame"), "dev", "gdb")

    assert mounts["category"] == "obb"
    assert 'CATEGORY = "obb"' in scripts[0]
    assert plan["plan"]["commands"][0] == "lepton gdb_server dev"

    with pytest.raises(DevkitAdapterError):
        adapter.lepton_mounts(DeviceRef("frame"), "dev", "unsupported")
    with pytest.raises(DevkitAdapterError):
        adapter.lepton_debug_plan(DeviceRef("frame"), "dev", "unsupported")


def test_steam_frame_manager_interfaces_uses_plural_property_bucket(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        del resolved
        scripts.append(script)
        return {"service": "com.steampowered.SteamOSManager1", "paths": {}}

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    adapter.steam_frame_manager_interfaces(DeviceRef("frame"), include_system=True)

    assert '"property": "properties"' in scripts[0]
    assert 'KIND_BUCKETS[kind]' in scripts[0]
