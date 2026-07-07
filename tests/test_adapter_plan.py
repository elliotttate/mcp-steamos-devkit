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
    (source / "b.bin").write_bytes(b"abcde")

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


def test_steampipe_android_release_preflight_checks_layout(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "game"
    source.mkdir()
    apk = source / "game.apk"
    apk.write_bytes(b"fake apk")
    (source / "game.main.obb").write_bytes(b"root obb")
    obb_dir = source / "obb"
    obb_dir.mkdir()
    (obb_dir / "content_001.obb").write_bytes(b"content")
    adapter = make_adapter(tmp_path)

    monkeypatch.setattr(
        adapter,
        "inspect_android_apk",
        lambda apk_path: {
            "apk_path": apk_path,
            "package_name": "com.example.game",
            "version_code": "42",
            "expected_main_obb": "main.42.com.example.game.obb",
        },
    )

    result = adapter.steampipe_android_release_preflight(
        str(source),
        app_id="555000",
        depot_id="555001",
        cloud_subdirectory="com.example.game",
    )

    assert result["ok"]
    assert result["launch_executable"] == "game.apk"
    assert "Root-level OBB files were found" in result["warnings"][0]
    assert result["checklist"][2]["status"] == "ok"
    assert result["checklist"][-1]["status"] == "ok"


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


def test_second_pass_status_tools_build_expected_remote_scripts(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str) -> dict[str, Any]:
        del resolved
        scripts.append(script)
        if "native_user_active_runtime" in script:
            return {"paths": {}}
        if "ENABLE_VULKAN_RENDERDOC_CAPTURE" in script:
            return {"helpers": {}, "vulkan_layers": {}, "files": {}, "env_flags": {}}
        if "patterns = [" in script:
            return {"context": "dev", "package_name": "com.example.game", "entries": []}
        return {"root": "/home/steamos/.config/openvr/config/cv/xrservice/datasets", "datasets": []}

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    adapter.steam_frame_openxr_status(DeviceRef("frame"))
    adapter.lepton_graphics_debug_status(DeviceRef("frame"))
    adapter.lepton_artifacts_manifest(DeviceRef("frame"), "dev", "com.example.game", limit=999)
    adapter.steam_frame_tracking_datasets(DeviceRef("frame"), limit=999)

    assert "native_user_active_runtime" in scripts[0]
    assert "lepton_overlay_active_runtime" in scripts[0]
    assert "ENABLE_VULKAN_RENDERDOC_CAPTURE" in scripts[1]
    assert "mesa_version" in scripts[1]
    assert 'CONTEXT = "dev"' in scripts[2]
    assert 'PACKAGE_NAME = "com.example.game"' in scripts[2]
    assert "LIMIT = 500" in scripts[2]
    assert "xrservice/datasets" in scripts[3]
    assert "LIMIT = 100" in scripts[3]

    with pytest.raises(DevkitAdapterError):
        adapter.lepton_artifacts_manifest(DeviceRef("frame"), "dev", "not a package")


def test_adb_environment_conflict_doctor_reports_conflicts(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    adb_one = tmp_path / "sdk1" / "adb.exe"
    adb_two = tmp_path / "sdk2" / "adb.exe"
    adb_one.parent.mkdir()
    adb_two.parent.mkdir()
    adb_one.write_text("fake", encoding="utf-8")
    adb_two.write_text("fake", encoding="utf-8")

    monkeypatch.setenv("ADB_PATH", str(adb_one))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(adapter.layout, "locate_adb", lambda: str(adb_two))
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.0.2.10", 5555))],
    )
    monkeypatch.setattr(
        adapter,
        "_run_adb",
        lambda args, timeout: {
            "args": args,
            "timeout": timeout,
            "returncode": 0,
            "stdout": "List of devices attached\nframe:5555\tdevice\nother:5555\tdevice\n",
            "stderr": "",
        },
    )

    def fake_run(cmd, **kwargs):
        del kwargs
        if cmd[0] in {str(adb_one), str(adb_two)}:
            return type(
                "Proc",
                (),
                {"returncode": 0, "stdout": f"Android Debug Bridge version {cmd[0]}\n", "stderr": ""},
            )()
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = adapter.adb_environment_conflict_doctor("frame")

    assert not result["ok"]
    assert len(result["candidates"]) == 2
    assert result["dns"]["addresses"] == ["192.0.2.10"]
    assert any("Multiple adb executables" in note for note in result["notes"])
    assert any("Multiple TCP ADB targets" in note for note in result["notes"])


def test_sync_tracking_dataset_downloads_latest_dataset(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    transfers: list[tuple[str, str]] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)
    monkeypatch.setattr(
        adapter,
        "steam_frame_tracking_datasets",
        lambda ref, limit=100: {
            "root": "/home/steamos/.config/openvr/config/cv/xrservice/datasets",
            "datasets": [
                {
                    "name": "dataset-new",
                    "path": "/home/steamos/.config/openvr/config/cv/xrservice/datasets/dataset-new",
                }
            ],
        },
    )

    def fake_rsync_transfer(
        localdir: str,
        resolved: DeviceInfo,
        remotedir: str | list[str],
        upload: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del resolved, kwargs
        transfers.append((localdir, str(remotedir)))
        assert not upload
        return {"returncode": 0}

    monkeypatch.setattr(adapter, "rsync_transfer", fake_rsync_transfer)

    result = adapter.sync_tracking_dataset(DeviceRef("frame"), str(tmp_path / "datasets"))

    assert result["dataset"]["name"] == "dataset-new"
    assert transfers == [
        (
            str(tmp_path / "datasets"),
            "/home/steamos/.config/openvr/config/cv/xrservice/datasets/dataset-new",
        )
    ]


def test_local_steamvr_automation_inventory_reads_driver_surfaces(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    steamvr = tmp_path / "SteamVR"
    null_settings = steamvr / "drivers" / "null" / "resources" / "settings" / "default.vrsettings"
    frame_input = steamvr / "drivers" / "frame_controller" / "resources" / "input"
    null_settings.parent.mkdir(parents=True)
    frame_input.mkdir(parents=True)
    null_settings.write_text('{"driver_null": {"enable": false}}', encoding="utf-8")
    (frame_input / "frame_controller_profile.json").write_text(
        """
{
  "controller_type": "frame_controller",
  "input_source": {
    "/input/grip/pose": {"type": "pose"},
    "/input/trigger/value": {"type": "scalar"}
  }
}
""",
        encoding="utf-8",
    )
    (frame_input / "binding_vrmonitor.json").write_text("{}", encoding="utf-8")

    result = adapter.local_steamvr_automation_inventory(str(steamvr))

    assert result["ok"]
    assert "null" in result["drivers"]
    assert result["null_driver"]["exists"]
    assert result["null_driver"]["default_settings"]["driver_null"]["enable"] is False
    assert result["frame_controller"]["controller_type"] == "frame_controller"
    assert result["frame_controller"]["pose_paths"] == ["/input/grip/pose"]
    assert "binding_vrmonitor.json" in result["frame_controller"]["binding_files"]
    assert any(cap["name"] == "pc_synthetic_hmd" and cap["available"] for cap in result["capabilities"])
    assert any(
        cap["name"] == "controller_pose_replay" and not cap["available"]
        for cap in result["capabilities"]
    )


def test_steam_frame_automation_inventory_builds_expected_remote_script(tmp_path: Path, monkeypatch) -> None:
    adapter = make_adapter(tmp_path)
    device = DeviceInfo(id="frame", name="frame", address="frame", login="steamos")
    scripts: list[str] = []

    monkeypatch.setattr(adapter, "resolve_device", lambda ref: device)

    def fake_remote_python_json(resolved: DeviceInfo, script: str, **kwargs: Any) -> dict[str, Any]:
        del resolved, kwargs
        scripts.append(script)
        return {
            "steamvr": {"path": "/home/steamos/.steam/steam/steamapps/common/SteamVR"},
            "tracking_datasets": {"root": "/home/steamos/.config/openvr/config/cv/xrservice/datasets"},
            "tools": {"adb": "/usr/bin/adb", "evemu-record": None},
            "capabilities": {"android_input_via_adb": True},
        }

    monkeypatch.setattr(adapter, "_remote_python_json", fake_remote_python_json)

    result = adapter.steam_frame_automation_inventory(DeviceRef("frame"))

    assert result["device"]["name"] == "frame"
    assert result["capabilities"]["android_input_via_adb"]
    assert "xrservice/datasets" in scripts[0]
    assert "evemu-record" in scripts[0]
    assert "steamvr_binaries" in scripts[0]
    assert "lepton_help" in scripts[0]
    assert any("Tracking datasets" in note for note in result["notes"])


def test_steam_frame_automation_plan_covers_pose_replay(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = adapter.steam_frame_automation_plan("pose-replay")

    pose_plan = result["plans"]["pose_replay"]
    assert pose_plan["status"] == "not_directly_available_yet"
    assert "sync_tracking_dataset" in pose_plan["mcp_tools"]
    assert any("custom OpenVR server driver" in option for option in pose_plan["next_build_options"])

    with pytest.raises(DevkitAdapterError):
        adapter.steam_frame_automation_plan("unknown")


def test_steamvr_vrcmd_capability_inventory_reads_binary_tokens(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    steamvr = tmp_path / "SteamVR"
    for bin_dir in [steamvr / "bin" / "win64", steamvr / "bin" / "linux64"]:
        bin_dir.mkdir(parents=True)
        (bin_dir / "vrcmd.exe").write_bytes(b"--pollposes --replay --startcapture --send-vrevent")
        (bin_dir / "vrserver.exe").write_bytes(b"simulate_hmd forcedDriver activateMultipleDrivers")
        (bin_dir / "vrpathreg.exe").write_bytes(b"")
        (bin_dir / "vrcmd").write_bytes(b"--pollposes --replay --startcapture --send-vrevent")
        (bin_dir / "vrserver").write_bytes(b"simulate_hmd forcedDriver activateMultipleDrivers")
        (bin_dir / "vrpathreg").write_bytes(b"")

    result = adapter.steamvr_vrcmd_capability_inventory(str(steamvr))

    assert result["ok"]
    assert "--replay" in result["categories"]["capture_replay"]
    assert "--pollposes" in result["categories"]["pose_polling"]
    assert "--send-vrevent" in result["categories"]["event_injection"]
    assert "simulate_hmd" in result["categories"]["driver_simulation"]
    assert any(
        cap["name"] == "host_capture_replay_tokens" and cap["available"]
        for cap in result["capabilities"]
    )


def test_tracking_dataset_analyze_summarizes_capture_files(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    dataset = tmp_path / "dataset"
    (dataset / "camera").mkdir(parents=True)
    (dataset / "hmd_pose.bin").write_bytes(b"pose")
    (dataset / "controller_pose.json").write_text("{}", encoding="utf-8")
    (dataset / "camera" / "frame.raw").write_bytes(b"camera")
    (dataset / "audio.wav").write_bytes(b"audio")

    result = adapter.tracking_dataset_analyze(str(dataset))

    assert result["kind"] == "directory"
    assert result["file_count"] == 4
    assert result["signal_counts"]["hmd"] == 1
    assert result["signal_counts"]["controller"] == 1
    assert result["signal_counts"]["pose"] == 2
    assert result["signal_counts"]["audio"] == 1
    assert result["total_bytes"] > 0


def test_steam_frame_replay_script_template_documents_pose_driver_future(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = adapter.steam_frame_replay_script_template("pose-driver-replay")

    template = result["templates"]["pose_driver_replay"]
    assert template["status"] == "future_custom_driver_required"
    assert template["template"]["actors"] == ["hmd", "left_controller", "right_controller"]
    assert any(step.get("actor") == "right_controller" for step in template["template"]["steps"])

    with pytest.raises(DevkitAdapterError):
        adapter.steam_frame_replay_script_template("unknown")
