# MCP SteamOS Devkit

`mcp-steamos-devkit` is a local MCP server for SteamOS Devkit Client devices. It wraps Valve's current Devkit Client model where possible and includes a native fallback for the core HTTP, SSH, rsync, and remote helper-script flow.

The server is built for Windows-first SteamOS Devkit installs, but the core adapter also works on Linux/macOS when `ssh`, `rsync`, and the Devkit helper scripts are available.

## What It Exposes

- Device discovery over `_steamos-devkit._tcp.local`
- Resolve by mDNS name, hostname, or IP
- Device properties and `steamos-get-status --json`
- Pairing/registration using the Devkit HTTP `/register` flow
- SSH key generation and Windows private-key ACL repair
- Sync of `devkit-utils` to `~/devkit-utils`
- Title upload with rsync and Steam shortcut registration
- Title launch through `steam-devkit-rpc run-game`
- Steam client/session controls
- Logs, controller config dumps, screenshots, GPU traces, RGP captures, RenderDoc replay server control
- Steam Frame native diagnostics: Lepton container/debug-port inventory, Steam/SteamVR/OpenXR log manifests, systemd service listing, journal tails, and bounded OS/Lepton/static-binary inventory for MCP tool discovery
- Steam Frame Lepton ADB helpers: device listing, Wi-Fi/USB connect, forward/reverse, APK install, shell, logcat, bugreport, and Unreal Insights setup
- Steam Frame Unity split APK/OBB helpers: APK package inspection, OBB layout validation/staging, and focused launch diagnostics
- Safety confirmation tokens for destructive or arbitrary remote operations

## Quick Start

```powershell
cd C:\Users\ellio\Documents\Codex\2026-07-07\come\outputs\mcp-steamos-devkit
python -m pip install -e .[dev]
mcp-steamos-devkit doctor
mcp-steamos-devkit serve
```

If the server cannot find the installed SteamOS Devkit Client, set:

```powershell
$env:STEAMOS_DEVKIT_CLIENT_ROOT = 'E:\SteamLibrary\steamapps\common\SteamOSDevkitClient\windows-client'
```

Optional source checkout:

```powershell
$env:STEAMOS_DEVKIT_SOURCE_ROOT = 'C:\path\to\steamos-devkit'
```

Optional ADB path for Steam Frame Lepton/Android work:

```powershell
$env:ADB_PATH = 'C:\Users\ellio\AppData\Local\Android\Sdk\platform-tools\adb.exe'
```

Optional SSH password fallback if the device has not accepted the devkit SSH key:

```powershell
$env:STEAMOS_DEVKIT_SSH_PASSWORD = 'your-device-password'
```

Optional Android build-tools path for APK metadata inspection:

```powershell
$env:AAPT_PATH = 'C:\Users\ellio\AppData\Local\Android\Sdk\build-tools\36.1.0\aapt.exe'
```

## Steam Frame ADB

Steam's bundled docs describe ADB as targeting the Lepton Android container on Steam Frame, not the native SteamOS Linux shell. The container is not always running; launch **Lepton Development** or any Android game first.

Wi-Fi flow:

```text
adb_connect_lepton_wifi(host="frame", port=5555)
adb_devices()
adb_logcat(serial="frame:5555")
```

USB flow:

```text
adb_connect_lepton_usb(local_port=5555, remote_port=5555)
adb_devices()
```

Useful follow-ups:

```text
adb_install_apk(apk_path="C:\path\game.apk", serial="frame:5555")
adb_bugreport(output_path="C:\path\frame-bugreport.zip", serial="frame:5555")
adb_unreal_insights_setup(tracehost="127.0.0.1", port=1981, serial="frame:5555")
adb_lepton_app_diagnostics(package_name="com.example.game", serial="frame:5556")
```

`adb_shell` is intentionally confirmation-gated because it can run arbitrary commands inside the Android container.

## Steam Frame Native Diagnostics

These read-only tools inspect the native SteamOS side of Steam Frame:

```text
lepton_containers(target="frame")
lepton_logcat(target="frame", context="steamlaunch-3570175983", lines=300)
steam_logs_manifest(target="frame", pattern="xrclient", limit=20)
steam_frame_perfcriteria(target="frame")
steam_frame_cef_pages(target="frame")
steam_frame_web_ports(target="frame")
steam_frame_dbus_manager(target="frame")
native_adbd_status(target="frame")
coredump_list(target="frame", limit=20)
steam_services(target="frame", scope="user", pattern="steamvr")
journalctl_tail(target="frame", unit="steamvr.service", scope="user", lines=200)
steam_frame_dev_inventory(target="frame")
```

`steam_frame_dev_inventory` is a bounded discovery pass for future MCP controls. It collects OS build
metadata, Lepton CLI help, Lepton script functions/env references, relevant system/user services,
DBus names, and static strings from known Steam Frame helper binaries. It does not kill, restart,
trace, or mutate Steam/Lepton state.

DBus control methods, Lepton debug-server lifecycle, RenderDoc/Vulkan layer injection, tracking
dataset packaging, Mesa debug package installation, and coredump debugger backtraces should be
implemented as separate confirmation-gated tools because they start capture/debug flows, alter
runtime state, or may expose private data.

## Steam Frame Unity APK + OBB

Unity split builds can export an APK plus a root-level file named like `game.main.obb`.
For Android expansion-file loading, the OBB must be visible to the app as:

```text
/sdcard/Android/obb/<package-name>/main.<versionCode>.<package-name>.obb
```

Steam Frame Lepton maps `/sdcard/Android/obb/<package-name>` to the title's `obb/`
directory when that directory exists, so Steam Frame packages should use:

```text
game.apk
obb/main.<versionCode>.<package-name>.obb
```

Useful flow:

```text
inspect_android_apk(apk_path="C:\path\build\game.apk")
validate_android_split_package(local_dir="C:\path\build")
stage_android_obb_layout(local_dir="C:\path\build")
upload_title(target="frame", gameid="mygame", local_dir="C:\path\build", runtime="android")
run_title(target="frame", gameid="mygame")
adb_lepton_app_diagnostics(package_name="com.example.game", serial="frame:5556")
```

The diagnostics helper reports the app PID/activity state, the OBB symlink target,
`SteamAppId`/`SteamGameId` process environment, and logcat highlights for OpenXR,
Steamworks, TMP, exceptions, and common Unity XR failure strings.

## MCP Client Config

```json
{
  "mcpServers": {
    "steamos-devkit": {
      "command": "python",
      "args": ["-m", "mcp_steamos_devkit", "serve"],
      "env": {
        "STEAMOS_DEVKIT_CLIENT_ROOT": "E:\\SteamLibrary\\steamapps\\common\\SteamOSDevkitClient\\windows-client"
      }
    }
  }
}
```

## Safety Model

The server treats these operations as confirmation-gated:

- Pairing/registering a device
- Clean uploads that delete remote files
- Local artifact writes such as log syncs and ADB bugreports
- Deleting titles or all titles
- Resetting Steam client state
- Restarting sessions or rebooting
- Clearing Android logcat before capture
- Arbitrary SSH or Steam RPC
- Arbitrary ADB shell commands

Call the tool once without a token to get a `requires_confirmation` response. Re-run with the returned `confirmation_token` to execute the same operation.

## Notes

The installed Windows client currently ships Python 3.14 bytecode. This package does not require importing that bytecode. It uses Valve-compatible HTTP/SSH/rsync behavior directly, and can also use a source checkout when one is configured.
