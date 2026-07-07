from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from .adapter import SteamOSDevkitAdapter
from .config import find_layout
from .models import DeviceNameType, DeviceRef, SafetyLevel, UploadProfile, redact, to_jsonable
from .operations import OperationManager
from .safety import ConfirmationManager, upload_safety
from .state import JsonStore

SAFE_RPC_COMMANDS = {"run-game"}


def create_server() -> FastMCP:
    layout = find_layout()
    store = JsonStore(layout.data_dir)
    adapter = SteamOSDevkitAdapter(layout=layout, store=store)
    confirmations = ConfirmationManager(store)
    operations = OperationManager(store)

    mcp = FastMCP(
        "SteamOS Devkit",
        instructions=(
            "Use this server to discover, pair, deploy, run, and collect diagnostics from "
            "SteamOS Devkit Client devices. It also exposes ADB helpers for the Steam Frame "
            "Lepton Android container. Destructive and arbitrary-execution tools return a "
            "confirmation token on first call; repeat with that token to execute."
        ),
    )

    @mcp.resource("steamos-devkit://server/info")
    def server_info() -> dict[str, Any]:
        return {"name": "mcp-steamos-devkit", "layout": layout.doctor()}

    @mcp.resource("steamos-devkit://server/doctor")
    def server_doctor() -> dict[str, Any]:
        return adapter.doctor()

    @mcp.resource("steamos-devkit://devices")
    def devices_resource() -> list[dict[str, Any]]:
        return list(store.load("devices", {}).values())

    @mcp.resource("steamos-devkit://profiles")
    def profiles_resource() -> dict[str, Any]:
        return store.load("profiles", {})

    @mcp.resource("steamos-devkit://operations")
    def operations_resource() -> list[dict[str, Any]]:
        return operations.list()

    @mcp.resource("steamos-devkit://help/safety")
    def safety_help() -> dict[str, Any]:
        return {
            "read_only": [
                "discover_devices",
                "resolve_device",
                "get_steamos_status",
                "list_titles",
                "adb_doctor",
                "adb_devices",
                "adb_logcat without clear_first",
                "adb_bugreport",
                "inspect_android_apk",
                "validate_android_split_package",
                "adb_lepton_app_diagnostics",
            ],
            "write": [
                "sync_devkit_utils",
                "upload_title without clean delete",
                "run_title",
                "ADB connect/disconnect/forward/reverse",
                "adb_install_apk",
                "adb_unreal_insights_setup",
                "stage_android_obb_layout",
            ],
            "destructive": [
                "register_device",
                "clean upload",
                "delete_title",
                "restart_session",
                "reboot_device",
                "adb_logcat with clear_first",
            ],
            "arbitrary_execution": ["run_remote_command", "adb_shell", "uncurated steam_rpc"],
        }

    @mcp.resource("steamos-devkit://help/android-split-obb")
    def android_split_obb_help() -> dict[str, Any]:
        return {
            "scope": "Steam Frame Unity APK + OBB split packages",
            "naming_rule": "main.<android:versionCode>.<package-name>.obb",
            "steam_frame_layout": "Place the OBB at obb/main.<versionCode>.<package>.obb next to the top-level APK.",
            "why": (
                "Lepton maps /sdcard/Android/obb/<package> to the app's obb/ directory "
                "when it exists. Unity still expects the Android expansion filename."
            ),
            "tools": [
                "inspect_android_apk to read package/versionCode with aapt",
                "validate_android_split_package to detect missing or misnamed OBBs",
                "stage_android_obb_layout to copy a Unity *.main.obb export to the expected name",
                "adb_lepton_app_diagnostics after launch to verify process, OBB symlink, Steam env, and XR logs",
            ],
        }

    @mcp.resource("steamos-devkit://help/adb")
    def adb_help() -> dict[str, Any]:
        return {
            "scope": "Steam Frame Lepton Android container, not the native SteamOS Linux shell",
            "prerequisites": [
                "Enable Developer Mode on Steam Frame.",
                "Start Lepton by launching Lepton Development or any Android game.",
                "Install Android SDK Platform Tools or set ADB_PATH.",
            ],
            "wifi": "adb_connect_lepton_wifi defaults to frame:5555.",
            "usb": "adb_connect_lepton_usb forwards host tcp:5555 to the native USB ADB target, then connects localhost:5555.",
            "common_followups": [
                "adb_devices to confirm state",
                "adb_logcat for Android logs",
                "adb_lepton_app_diagnostics for app pid/activity/OBB/env and XR/Steam log highlights",
                "adb_bugreport for a zip/text bugreport artifact",
                "adb_unreal_insights_setup to set debug.ue.commandline and reverse tcp:1981",
            ],
        }

    @mcp.tool()
    def doctor() -> dict[str, Any]:
        """Inspect local SteamOS Devkit Client, tools, dependencies, and state paths."""
        return adapter.doctor()

    @mcp.tool()
    def adb_doctor() -> dict[str, Any]:
        """Inspect local ADB availability and print Steam Frame Lepton connection notes."""
        return _run_tool(operations, "adb_doctor", SafetyLevel.READ_ONLY, adapter.adb_doctor)

    @mcp.tool()
    def discover_devices(timeout_seconds: float = 5, include_cached: bool = True) -> dict[str, Any]:
        """Discover SteamOS Devkit devices by mDNS and optionally include cached devices."""
        return _run_tool(
            operations,
            "discover_devices",
            SafetyLevel.READ_ONLY,
            lambda: adapter.discover_devices(timeout_seconds, include_cached),
        )

    @mcp.tool()
    def resolve_device(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        refresh_properties: bool = True,
    ) -> dict[str, Any]:
        """Resolve a SteamOS Devkit target by service name, hostname, or IP address."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "resolve_device",
            SafetyLevel.READ_ONLY,
            lambda: to_jsonable(adapter.resolve_device(ref, refresh_properties)),
        )

    @mcp.tool()
    def ensure_ssh_key() -> dict[str, Any]:
        """Create or repair the local SteamOS Devkit SSH key."""
        return _run_tool(
            operations,
            "ensure_ssh_key",
            SafetyLevel.WRITE,
            lambda: redact(adapter.ensure_ssh_key()),
        )

    @mcp.tool()
    def register_device(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Register/pair this machine's Devkit SSH key with a SteamOS device."""
        params = _params(locals())
        gate = confirmations.require(
            "register_device",
            params,
            SafetyLevel.DESTRUCTIVE,
            f"Register local SSH key with {target}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "register_device",
            SafetyLevel.DESTRUCTIVE,
            lambda: redact(adapter.register_device(ref)),
        )

    @mcp.tool()
    def sync_devkit_utils(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Sync Valve's devkit-utils helper scripts to ~/devkit-utils on the device."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "sync_devkit_utils",
            SafetyLevel.WRITE,
            lambda: adapter.sync_devkit_utils(ref),
        )

    @mcp.tool()
    def get_steamos_status(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Run steamos-get-status --json on the device."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "get_steamos_status",
            SafetyLevel.READ_ONLY,
            lambda: adapter.get_steamos_status(ref),
        )

    @mcp.tool()
    def list_titles(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """List devkit titles installed under ~/devkit-game."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "list_titles", SafetyLevel.READ_ONLY, lambda: {"titles": adapter.list_titles(ref)})

    @mcp.tool()
    def validate_upload_plan(
        target: str,
        gameid: str,
        local_dir: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        settings: dict[str, Any] | None = None,
        runtime: str | None = None,
        steam_play_debug: str = "disabled",
        delete_extraneous: bool = False,
        skip_newer_files: bool = False,
        verify_checksums: bool = False,
        filter_args: list[str] | None = None,
        restart_steam: bool = False,
        use_mask_unmask: bool = False,
        prevent_auto_repair: bool = False,
        gdbserver: bool = False,
    ) -> dict[str, Any]:
        """Validate a title upload plan without touching the device."""
        profile = _upload_profile(**locals())
        return _run_tool(
            operations,
            "validate_upload_plan",
            SafetyLevel.READ_ONLY,
            lambda: to_jsonable(adapter.validate_upload_plan(profile)),
        )

    @mcp.tool()
    def inspect_android_apk(apk_path: str) -> dict[str, Any]:
        """Read Android package/version metadata from an APK using aapt dump badging."""
        return _run_tool(
            operations,
            "inspect_android_apk",
            SafetyLevel.READ_ONLY,
            lambda: adapter.inspect_android_apk(apk_path),
        )

    @mcp.tool()
    def validate_android_split_package(
        local_dir: str,
        apk_name: str | None = None,
    ) -> dict[str, Any]:
        """Validate a Steam Frame Unity APK+OBB folder and expected OBB filename/layout."""
        return _run_tool(
            operations,
            "validate_android_split_package",
            SafetyLevel.READ_ONLY,
            lambda: adapter.validate_android_split_package(local_dir, apk_name),
        )

    @mcp.tool()
    def stage_android_obb_layout(
        local_dir: str,
        apk_name: str | None = None,
        source_obb: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy an Android main OBB into obb/main.<versionCode>.<package>.obb."""
        return _run_tool(
            operations,
            "stage_android_obb_layout",
            SafetyLevel.WRITE,
            lambda: adapter.stage_android_obb_layout(local_dir, apk_name, source_obb, overwrite),
        )

    @mcp.tool()
    def upload_title(
        target: str,
        gameid: str,
        local_dir: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        settings: dict[str, Any] | None = None,
        runtime: str | None = None,
        steam_play_debug: str = "disabled",
        delete_extraneous: bool = False,
        skip_newer_files: bool = False,
        verify_checksums: bool = False,
        filter_args: list[str] | None = None,
        restart_steam: bool = False,
        use_mask_unmask: bool = False,
        prevent_auto_repair: bool = False,
        gdbserver: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Upload a title folder, update runtime settings, and register/update the Steam shortcut."""
        params = _params(locals())
        safety = upload_safety(delete_extraneous)
        gate = confirmations.require(
            "upload_title",
            params,
            safety,
            f"Upload {local_dir} to {target} as {gameid}; clean delete={delete_extraneous}",
            confirmation_token,
        )
        if gate:
            gate["plan"] = to_jsonable(adapter.validate_upload_plan(_upload_profile(**locals())))
            return gate
        profile = _upload_profile(**locals())
        return _run_tool(operations, "upload_title", safety, lambda: adapter.upload_title(profile))

    @mcp.tool()
    def run_title(
        target: str,
        gameid: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Launch a devkit title through Steam's devkit RPC."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "run_title", SafetyLevel.WRITE, lambda: adapter.run_title(ref, gameid))

    @mcp.tool()
    def delete_title(
        target: str,
        gameid: str | None = None,
        delete_all: bool = False,
        reset_steam_client: bool = False,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Delete one or all devkit titles, optionally resetting Steam client state."""
        params = _params(locals())
        gate = confirmations.require(
            "delete_title",
            params,
            SafetyLevel.DESTRUCTIVE,
            f"Delete title data on {target}: gameid={gameid}, delete_all={delete_all}, reset={reset_steam_client}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "delete_title",
            SafetyLevel.DESTRUCTIVE,
            lambda: adapter.delete_title(ref, gameid, delete_all, reset_steam_client),
        )

    @mcp.tool()
    def set_steam_client(
        target: str,
        gameid: str,
        mode: str,
        args: str | None = None,
        gdbserver: bool = False,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Switch the Steam client mode used by the main session."""
        params = _params(locals())
        gate = confirmations.require(
            "set_steam_client",
            params,
            SafetyLevel.DESTRUCTIVE,
            f"Set Steam client on {target} to {mode}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "set_steam_client",
            SafetyLevel.DESTRUCTIVE,
            lambda: adapter.set_steam_client(ref, gameid, mode, args, gdbserver),
        )

    @mcp.tool()
    def set_session(
        target: str,
        session: str,
        wait: bool = True,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Change the SteamOS graphical session."""
        params = _params(locals())
        gate = confirmations.require(
            "set_session",
            params,
            SafetyLevel.DESTRUCTIVE,
            f"Switch {target} session to {session}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "set_session", SafetyLevel.DESTRUCTIVE, lambda: adapter.set_session(ref, session, wait))

    @mcp.tool()
    def restart_session(
        target: str,
        is_deckard: bool | None = None,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Restart the active SteamOS session or Steam user service."""
        params = _params(locals())
        gate = confirmations.require("restart_session", params, SafetyLevel.DESTRUCTIVE, f"Restart session on {target}", confirmation_token)
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "restart_session", SafetyLevel.DESTRUCTIVE, lambda: adapter.restart_session(ref, is_deckard))

    @mcp.tool()
    def reboot_device(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Reboot the SteamOS device."""
        params = _params(locals())
        gate = confirmations.require("reboot_device", params, SafetyLevel.DESTRUCTIVE, f"Reboot {target}", confirmation_token)
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "reboot_device", SafetyLevel.DESTRUCTIVE, lambda: adapter.reboot_device(ref))

    @mcp.tool()
    def enable_cef_debugging(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Enable Steam CEF remote debugging for the device's Steam client."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "enable_cef_debugging", SafetyLevel.WRITE, lambda: adapter.enable_cef_debugging(ref))

    @mcp.tool()
    def open_cef_console(
        target: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Return the Steam CEF debugging URL for a device."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "open_cef_console", SafetyLevel.READ_ONLY, lambda: adapter.open_cef_console(ref))

    @mcp.tool()
    def steam_rpc(
        target: str,
        command: str,
        params: dict[str, Any] | None = None,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute a Steam devkit RPC command. Uncurated commands require confirmation."""
        params = params or {}
        call_params = _params(locals())
        safety = SafetyLevel.WRITE if command in SAFE_RPC_COMMANDS else SafetyLevel.ARBITRARY_EXECUTION
        gate = confirmations.require(
            "steam_rpc",
            call_params,
            safety,
            f"Run Steam RPC {command} on {target}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "steam_rpc", safety, lambda: adapter.steam_rpc(ref, command, params))

    @mcp.tool()
    def run_remote_command(
        target: str,
        command: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Run an arbitrary SSH command on the device. Always requires confirmation."""
        params = _params(locals())
        gate = confirmations.require(
            "run_remote_command",
            params,
            SafetyLevel.ARBITRARY_EXECUTION,
            f"Run arbitrary command on {target}: {command}",
            confirmation_token,
        )
        if gate:
            return gate
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "run_remote_command",
            SafetyLevel.ARBITRARY_EXECUTION,
            lambda: adapter.run_remote_command(ref, command),
        )

    @mcp.tool()
    def dump_controller_config(
        target: str,
        output_folder: str,
        appid: str | None = None,
        gameid: str | None = None,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Dump controller configuration VDF files from Steam."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "dump_controller_config",
            SafetyLevel.WRITE,
            lambda: adapter.dump_controller_config(ref, output_folder, appid, gameid),
        )

    @mcp.tool()
    def sync_logs(
        target: str,
        local_folder: str,
        steamvr_logpath: str | None = None,
        device_name: str | None = None,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Sync Steam logs, SteamVR logs, and /tmp/dumps to a local folder."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "sync_logs",
            SafetyLevel.READ_ONLY,
            lambda: adapter.sync_logs(ref, local_folder, steamvr_logpath, device_name),
        )

    @mcp.tool()
    def screenshot(
        target: str,
        output_folder: str,
        filename: str | None = None,
        timestamp: bool = True,
        xprop: int = 1,
        is_deckard: bool | None = None,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Capture a SteamOS screenshot and download it locally."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "screenshot",
            SafetyLevel.WRITE,
            lambda: adapter.screenshot(ref, output_folder, filename, timestamp, xprop, is_deckard),
        )

    @mcp.tool()
    def gpu_trace(
        target: str,
        local_filename: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Collect a gpu-trace zip from the device."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "gpu_trace", SafetyLevel.WRITE, lambda: adapter.gpu_trace(ref, local_filename))

    @mcp.tool()
    def rgp_capture(
        target: str,
        output_folder: str,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Trigger and download a Radeon GPU Profiler capture."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(operations, "rgp_capture", SafetyLevel.WRITE, lambda: adapter.rgp_capture(ref, output_folder))

    @mcp.tool()
    def set_renderdoc_replay(
        target: str,
        enabled: bool,
        login: str | None = None,
        http_port: int = 32000,
        name_type: str = "guess",
    ) -> dict[str, Any]:
        """Start or stop the RenderDoc replay server on the device."""
        ref = _device_ref(target, login, http_port, name_type)
        return _run_tool(
            operations,
            "set_renderdoc_replay",
            SafetyLevel.WRITE,
            lambda: adapter.set_renderdoc_replay(ref, enabled),
        )

    @mcp.tool()
    def adb_devices() -> dict[str, Any]:
        """List ADB devices and parse adb devices -l output."""
        return _run_tool(operations, "adb_devices", SafetyLevel.READ_ONLY, adapter.adb_devices)

    @mcp.tool()
    def adb_connect_lepton_wifi(host: str = "frame", port: int = 5555) -> dict[str, Any]:
        """Connect to Steam Frame's Lepton Android container over Wi-Fi."""
        return _run_tool(
            operations,
            "adb_connect_lepton_wifi",
            SafetyLevel.WRITE,
            lambda: adapter.adb_connect_lepton_wifi(host, port),
        )

    @mcp.tool()
    def adb_connect_lepton_usb(local_port: int = 5555, remote_port: int = 5555) -> dict[str, Any]:
        """Connect to Steam Frame's Lepton Android container through USB ADB port forwarding."""
        return _run_tool(
            operations,
            "adb_connect_lepton_usb",
            SafetyLevel.WRITE,
            lambda: adapter.adb_connect_lepton_usb(local_port, remote_port),
        )

    @mcp.tool()
    def adb_disconnect(target: str | None = None) -> dict[str, Any]:
        """Disconnect one ADB target, or all TCP ADB targets when target is omitted."""
        return _run_tool(
            operations,
            "adb_disconnect",
            SafetyLevel.WRITE,
            lambda: adapter.adb_disconnect(target),
        )

    @mcp.tool()
    def adb_forward(local_port: int, remote_port: int, serial: str | None = None) -> dict[str, Any]:
        """Forward host tcp:LOCAL_PORT to device tcp:REMOTE_PORT with adb forward."""
        return _run_tool(
            operations,
            "adb_forward",
            SafetyLevel.WRITE,
            lambda: adapter.adb_forward(local_port, remote_port, serial),
        )

    @mcp.tool()
    def adb_reverse(device_port: int, host_port: int, serial: str | None = None) -> dict[str, Any]:
        """Reverse device tcp:DEVICE_PORT to host tcp:HOST_PORT with adb reverse."""
        return _run_tool(
            operations,
            "adb_reverse",
            SafetyLevel.WRITE,
            lambda: adapter.adb_reverse(device_port, host_port, serial),
        )

    @mcp.tool()
    def adb_install_apk(
        apk_path: str,
        serial: str | None = None,
        replace: bool = True,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Install or replace an APK in the connected Lepton Android container."""
        return _run_tool(
            operations,
            "adb_install_apk",
            SafetyLevel.WRITE,
            lambda: adapter.adb_install_apk(apk_path, serial, replace, extra_args),
        )

    @mcp.tool()
    def adb_shell(
        command: str,
        serial: str | None = None,
        timeout: int = 60,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Run an arbitrary non-interactive adb shell command. Always requires confirmation."""
        params = _params(locals())
        gate = confirmations.require(
            "adb_shell",
            params,
            SafetyLevel.ARBITRARY_EXECUTION,
            f"Run adb shell command: {command}",
            confirmation_token,
        )
        if gate:
            return gate
        return _run_tool(
            operations,
            "adb_shell",
            SafetyLevel.ARBITRARY_EXECUTION,
            lambda: adapter.adb_shell(command, serial, timeout),
        )

    @mcp.tool()
    def adb_logcat(
        serial: str | None = None,
        lines: int = 200,
        filter_args: list[str] | None = None,
        clear_first: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Collect a bounded adb logcat dump; clear_first requires confirmation."""
        params = _params(locals())
        safety = SafetyLevel.DESTRUCTIVE if clear_first else SafetyLevel.READ_ONLY
        gate = confirmations.require(
            "adb_logcat",
            params,
            safety,
            "Clear Android logcat buffer before collecting logs" if clear_first else "Collect Android logcat",
            confirmation_token,
        )
        if gate:
            return gate
        return _run_tool(
            operations,
            "adb_logcat",
            safety,
            lambda: adapter.adb_logcat(serial, lines, filter_args, clear_first),
        )

    @mcp.tool()
    def adb_bugreport(output_path: str, serial: str | None = None) -> dict[str, Any]:
        """Collect adb bugreport into a local file or folder path."""
        return _run_tool(
            operations,
            "adb_bugreport",
            SafetyLevel.READ_ONLY,
            lambda: adapter.adb_bugreport(output_path, serial),
        )

    @mcp.tool()
    def adb_unreal_insights_setup(
        tracehost: str = "127.0.0.1",
        port: int = 1981,
        serial: str | None = None,
    ) -> dict[str, Any]:
        """Set Unreal's Android tracehost property and reverse tcp:1981 for Unreal Insights."""
        return _run_tool(
            operations,
            "adb_unreal_insights_setup",
            SafetyLevel.WRITE,
            lambda: adapter.adb_unreal_insights_setup(tracehost, port, serial),
        )

    @mcp.tool()
    def adb_lepton_app_diagnostics(
        package_name: str,
        serial: str | None = None,
        log_lines: int = 400,
    ) -> dict[str, Any]:
        """Collect focused Steam Frame Lepton app state, OBB, Steam env, and XR/Steam log highlights."""
        return _run_tool(
            operations,
            "adb_lepton_app_diagnostics",
            SafetyLevel.READ_ONLY,
            lambda: adapter.adb_lepton_app_diagnostics(package_name, serial, log_lines),
        )

    @mcp.prompt()
    def deploy_and_run(gameid: str, local_dir: str, target: str) -> str:
        return (
            f"Validate and upload `{local_dir}` to `{target}` as `{gameid}`, then run the title. "
            "If upload asks for confirmation because clean delete is enabled, show the plan first."
        )

    @mcp.prompt()
    def pairing_troubleshooter(target: str) -> str:
        return (
            f"Troubleshoot pairing for `{target}`. Run doctor, resolve_device, ensure_ssh_key, "
            "then register_device if the target is reachable and the user confirms pairing."
        )

    @mcp.prompt()
    def collect_bug_report(target: str, output_folder: str) -> str:
        return (
            f"Collect SteamOS Devkit diagnostics for `{target}` into `{output_folder}`: status, "
            "title list, logs, and current server doctor output."
        )

    @mcp.prompt()
    def frame_adb_troubleshooter(host: str = "frame") -> str:
        return (
            f"Troubleshoot Steam Frame Lepton ADB for `{host}`. Run adb_doctor, confirm Developer "
            "Mode and that Lepton Development or an Android game is running, then try "
            "adb_connect_lepton_wifi and adb_devices. If Wi-Fi fails, guide the user through "
            "adb_connect_lepton_usb."
        )

    @mcp.prompt()
    def steam_frame_unity_split_troubleshooter(local_dir: str, package_name: str = "") -> str:
        return (
            f"Troubleshoot a Steam Frame Unity split APK+OBB package in `{local_dir}`. "
            "Run validate_android_split_package first. If the expected OBB is missing, run "
            "stage_android_obb_layout and re-upload the title. After launching, run "
            "adb_lepton_app_diagnostics"
            + (f" for `{package_name}`" if package_name else "")
            + " and check for the expected OBB file, XR_SESSION_STATE_SYNCHRONIZED, and "
            "SteamAPI_Init OK."
        )

    return mcp


def _run_tool(
    operations: OperationManager,
    name: str,
    safety: SafetyLevel,
    fn: Callable[[], Any],
) -> dict[str, Any]:
    op = operations.start(name=name, safety=safety)
    try:
        result = fn()
    except Exception as exc:
        operations.fail(op.id, str(exc))
        raise
    result = to_jsonable(result)
    operations.finish(op.id, result if isinstance(result, dict) else {"value": result})
    return {"operation_id": op.id, "result": result}


def _device_ref(
    target: str,
    login: str | None = None,
    http_port: int = 32000,
    name_type: str = "guess",
) -> DeviceRef:
    try:
        parsed_type = DeviceNameType(name_type)
    except ValueError:
        parsed_type = DeviceNameType.GUESS
    return DeviceRef(target=target, login=login, http_port=http_port, name_type=parsed_type)


def _upload_profile(**kwargs: Any) -> UploadProfile:
    return UploadProfile(
        device=_device_ref(kwargs["target"], kwargs.get("login"), kwargs.get("http_port", 32000), kwargs.get("name_type", "guess")),
        gameid=kwargs["gameid"],
        local_dir=str(Path(kwargs["local_dir"]).expanduser()),
        argv=kwargs.get("argv") or [],
        env=kwargs.get("env") or {},
        settings=kwargs.get("settings") or {},
        runtime=kwargs.get("runtime"),
        steam_play_debug=kwargs.get("steam_play_debug") or "disabled",
        delete_extraneous=bool(kwargs.get("delete_extraneous", False)),
        skip_newer_files=bool(kwargs.get("skip_newer_files", False)),
        verify_checksums=bool(kwargs.get("verify_checksums", False)),
        filter_args=kwargs.get("filter_args") or [],
        restart_steam=bool(kwargs.get("restart_steam", False)),
        use_mask_unmask=bool(kwargs.get("use_mask_unmask", False)),
        prevent_auto_repair=bool(kwargs.get("prevent_auto_repair", False)),
        gdbserver=bool(kwargs.get("gdbserver", False)),
    )


def _params(values: dict[str, Any]) -> dict[str, Any]:
    return {key: to_jsonable(value) for key, value in values.items() if key != "confirmation_token"}


def run(transport: str = "stdio") -> None:
    create_server().run(transport=transport)  # type: ignore[arg-type]
