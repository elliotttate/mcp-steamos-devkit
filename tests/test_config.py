from __future__ import annotations

from pathlib import Path

from mcp_steamos_devkit.config import DevkitLayout


def test_layout_finds_installed_client_tools(tmp_path: Path) -> None:
    root = tmp_path / "windows-client"
    (root / "cygroot" / "bin").mkdir(parents=True)
    (root / "cygroot" / "bin" / "ssh.exe").write_text("", encoding="utf-8")
    (root / "cygroot" / "bin" / "rsync.exe").write_text("", encoding="utf-8")
    (root / "cygroot" / "bin" / "cygpath.exe").write_text("", encoding="utf-8")
    (root / "devkit-utils").mkdir()

    layout = DevkitLayout(client_root=root, data_dir=tmp_path / "data", config_dir=tmp_path / "config")

    assert layout.devkit_utils_dir == root / "devkit-utils"
    assert layout.locate_tool("ssh.exe") == str(root / "cygroot" / "bin" / "ssh.exe")
    doctor = layout.doctor()
    assert doctor["devkit_utils_dir"] == str(root / "devkit-utils")


def test_layout_finds_adb_from_explicit_env(tmp_path: Path, monkeypatch) -> None:
    adb = tmp_path / "platform-tools" / "adb.exe"
    adb.parent.mkdir()
    adb.write_text("", encoding="utf-8")
    monkeypatch.setenv("ADB_PATH", str(adb))

    layout = DevkitLayout(client_root=None, data_dir=tmp_path / "data", config_dir=tmp_path / "config")

    assert layout.locate_adb() == str(adb)
    assert layout.doctor()["adb"] == str(adb)


def test_layout_finds_latest_aapt_from_android_sdk(tmp_path: Path, monkeypatch) -> None:
    old = tmp_path / "build-tools" / "34.0.0" / "aapt.exe"
    new = tmp_path / "build-tools" / "36.1.0" / "aapt.exe"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path))
    monkeypatch.delenv("AAPT_PATH", raising=False)

    layout = DevkitLayout(client_root=None, source_root=None, data_dir=tmp_path, config_dir=tmp_path)

    assert layout.locate_aapt() == str(new)
    assert layout.doctor()["aapt"] == str(new)


def test_layout_finds_source_client(tmp_path: Path) -> None:
    source = tmp_path / "steamos-devkit"
    (source / "client" / "devkit_client").mkdir(parents=True)
    (source / "client" / "devkit_client" / "__init__.py").write_text("", encoding="utf-8")
    (source / "client" / "devkit-utils").mkdir()

    layout = DevkitLayout(client_root=None, source_root=source, data_dir=tmp_path / "data", config_dir=tmp_path / "config")

    assert layout.python_source_client_dir == source / "client"
    assert layout.devkit_utils_dir == source / "client" / "devkit-utils"
