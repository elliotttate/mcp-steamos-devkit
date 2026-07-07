from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir


DEFAULT_WINDOWS_CLIENT_ROOT = Path(
    r"E:\SteamLibrary\steamapps\common\SteamOSDevkitClient\windows-client"
)


@dataclass
class DevkitLayout:
    client_root: Path | None
    source_root: Path | None = None
    data_dir: Path = field(default_factory=lambda: Path(user_data_dir("mcp-steamos-devkit", appauthor=False)))
    config_dir: Path = field(default_factory=lambda: Path(user_config_dir("steamos-devkit", appauthor=False)))

    @property
    def devkit_utils_dir(self) -> Path | None:
        for root in self._roots():
            for candidate in (root / "devkit-utils", root / "client" / "devkit-utils"):
                if candidate.is_dir():
                    return candidate
        return None

    @property
    def devkit_msvsmon_dir(self) -> Path | None:
        for root in self._roots():
            for candidate in (root / "devkit-msvsmon", root / "client" / "devkit-msvsmon"):
                if candidate.is_dir():
                    return candidate
        return None

    @property
    def python_source_client_dir(self) -> Path | None:
        for root in self._roots():
            for candidate in (root / "client", root):
                if (candidate / "devkit_client" / "__init__.py").is_file():
                    return candidate
        return None

    @property
    def bundled_cygwin_bin(self) -> Path | None:
        if self.client_root is None:
            return None
        candidate = self.client_root / "cygroot" / "bin"
        return candidate if candidate.is_dir() else None

    @property
    def ssh_key_path(self) -> Path:
        return self.config_dir / "devkit_rsa"

    @property
    def ssh_pubkey_path(self) -> Path:
        return self.config_dir / "devkit_rsa.pub"

    def locate_tool(self, name: str) -> str | None:
        if self.bundled_cygwin_bin is not None:
            candidate = self.bundled_cygwin_bin / name
            if candidate.is_file():
                return str(candidate)
        return shutil.which(name)

    def locate_adb(self) -> str | None:
        explicit = os.environ.get("ADB_PATH")
        if explicit and Path(explicit).is_file():
            return explicit
        found = shutil.which("adb.exe") or shutil.which("adb")
        if found:
            return found
        for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME"):
            sdk_root = os.environ.get(env_name)
            if sdk_root:
                candidate = Path(sdk_root) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
                if candidate.is_file():
                    return str(candidate)
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidate = Path(local_appdata) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
            if candidate.is_file():
                return str(candidate)
        return None

    def locate_aapt(self) -> str | None:
        explicit = os.environ.get("AAPT_PATH")
        if explicit and Path(explicit).is_file():
            return explicit
        found = shutil.which("aapt.exe") or shutil.which("aapt")
        if found:
            return found
        sdk_roots: list[Path] = []
        for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME"):
            sdk_root = os.environ.get(env_name)
            if sdk_root:
                sdk_roots.append(Path(sdk_root))
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            sdk_roots.append(Path(local_appdata) / "Android" / "Sdk")

        exe_name = "aapt.exe" if os.name == "nt" else "aapt"
        candidates: list[Path] = []
        for sdk_root in sdk_roots:
            build_tools = sdk_root / "build-tools"
            if build_tools.is_dir():
                candidates.extend(build_tools.glob(f"*/{exe_name}"))
        for candidate in sorted(candidates, reverse=True):
            if candidate.is_file():
                return str(candidate)
        return None

    def doctor(self) -> dict[str, Any]:
        return {
            "client_root": str(self.client_root) if self.client_root else None,
            "source_root": str(self.source_root) if self.source_root else None,
            "python_source_client_dir": (
                str(self.python_source_client_dir) if self.python_source_client_dir else None
            ),
            "devkit_utils_dir": str(self.devkit_utils_dir) if self.devkit_utils_dir else None,
            "devkit_msvsmon_dir": str(self.devkit_msvsmon_dir) if self.devkit_msvsmon_dir else None,
            "bundled_cygwin_bin": str(self.bundled_cygwin_bin) if self.bundled_cygwin_bin else None,
            "ssh": self.locate_tool("ssh.exe") or self.locate_tool("ssh"),
            "rsync": self.locate_tool("rsync.exe") or self.locate_tool("rsync"),
            "cygpath": self.locate_tool("cygpath.exe") or self.locate_tool("cygpath"),
            "adb": self.locate_adb(),
            "aapt": self.locate_aapt(),
            "config_dir": str(self.config_dir),
            "data_dir": str(self.data_dir),
            "ssh_key_path": str(self.ssh_key_path),
        }

    def _roots(self) -> list[Path]:
        roots: list[Path] = []
        if self.client_root:
            roots.append(self.client_root)
        if self.source_root and self.source_root not in roots:
            roots.append(self.source_root)
        return roots


def find_layout() -> DevkitLayout:
    client_root = _first_existing_path(
        os.environ.get("STEAMOS_DEVKIT_CLIENT_ROOT"),
        os.environ.get("STEAMOS_DEVKIT_ROOT"),
        DEFAULT_WINDOWS_CLIENT_ROOT,
    )
    source_root = _first_existing_path(
        os.environ.get("STEAMOS_DEVKIT_SOURCE_ROOT"),
        Path.cwd() / "work" / "steamos-devkit-official",
        Path.cwd().parent / "work" / "steamos-devkit-official",
    )
    data_dir = Path(
        os.environ.get(
            "MCP_STEAMOS_DEVKIT_DATA_DIR",
            user_data_dir("mcp-steamos-devkit", appauthor=False),
        )
    )
    config_dir = Path(
        os.environ.get(
            "STEAMOS_DEVKIT_CONFIG_DIR",
            user_config_dir("steamos-devkit", appauthor=False),
        )
    )
    return DevkitLayout(client_root=client_root, source_root=source_root, data_dir=data_dir, config_dir=config_dir)


def _first_existing_path(*values: object) -> Path | None:
    for value in values:
        if value is None:
            continue
        path = Path(value).expanduser()
        if path.exists():
            return path
    return None
