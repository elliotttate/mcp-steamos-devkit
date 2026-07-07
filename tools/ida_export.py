from __future__ import annotations

import json
import re
from pathlib import Path

import ida_auto
import ida_funcs
import ida_hexrays
import ida_name
import idautils
import idc


PATTERN = re.compile(
    r"steam|steamos|lepton|deckard|adb|debug|trace|perf|capture|renderdoc|"
    r"vulkan|openxr|gdb|lldb|dbus|busctl|service|journal|podman|container|"
    r"socket|manager|tdp|gpu|cpu|fan|battery|wifi|cec|session|coredump",
    re.IGNORECASE,
)


def main() -> None:
    out_dir = Path(idc.ARGV[1] if len(idc.ARGV) > 1 else ".").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "decompiled").mkdir(exist_ok=True)

    ida_auto.auto_wait()
    has_hexrays = bool(ida_hexrays.init_hexrays_plugin())

    functions = []
    for ea in idautils.Functions():
        func = ida_funcs.get_func(ea)
        if not func:
            continue
        functions.append(
            {
                "ea": f"0x{ea:x}",
                "name": ida_name.get_name(ea) or f"sub_{ea:x}",
                "size": int(func.end_ea - func.start_ea),
            }
        )

    strings = []
    interesting_funcs: dict[int, set[str]] = {}
    for item in idautils.Strings():
        text = str(item)
        entry = {"ea": f"0x{int(item.ea):x}", "text": text}
        if PATTERN.search(text):
            entry["interesting"] = True
            for xref in idautils.XrefsTo(item.ea):
                func = ida_funcs.get_func(xref.frm)
                if func:
                    interesting_funcs.setdefault(func.start_ea, set()).add(text[:200])
        strings.append(entry)

    imports = []
    for ordinal in range(idaapi_get_import_module_qty()):
        module = idaapi_get_import_module_name(ordinal)
        if not module:
            continue

        def visitor(ea: int, name: str, ord_: int) -> bool:
            imports.append({"module": module, "ea": f"0x{ea:x}", "name": name or "", "ordinal": ord_})
            return True

        idaapi_enum_import_names(ordinal, visitor)

    decompiled = []
    if has_hexrays:
        for index, (ea, refs) in enumerate(sorted(interesting_funcs.items())):
            if index >= 200:
                break
            func_name = ida_name.get_name(ea) or f"sub_{ea:x}"
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", func_name)[:96]
            try:
                cfunc = ida_hexrays.decompile(ea)
            except Exception as exc:
                decompiled.append({"ea": f"0x{ea:x}", "name": func_name, "error": str(exc), "refs": sorted(refs)})
                continue
            filename = f"{index:04d}_{ea:x}_{safe_name}.c"
            (out_dir / "decompiled" / filename).write_text(str(cfunc), encoding="utf-8", errors="replace")
            decompiled.append({"ea": f"0x{ea:x}", "name": func_name, "file": filename, "refs": sorted(refs)})

    summary = {
        "input": idc.get_input_file_path(),
        "hexrays": has_hexrays,
        "function_count": len(functions),
        "string_count": len(strings),
        "interesting_function_count": len(interesting_funcs),
        "decompiled_count": len([item for item in decompiled if "file" in item]),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "functions.json").write_text(json.dumps(functions, indent=2), encoding="utf-8")
    (out_dir / "strings.json").write_text(json.dumps(strings, indent=2), encoding="utf-8")
    (out_dir / "imports.json").write_text(json.dumps(imports, indent=2), encoding="utf-8")
    (out_dir / "decompiled_index.json").write_text(json.dumps(decompiled, indent=2), encoding="utf-8")
    idc.qexit(0)


def idaapi_get_import_module_qty() -> int:
    import ida_nalt

    return ida_nalt.get_import_module_qty()


def idaapi_get_import_module_name(ordinal: int) -> str | None:
    import ida_nalt

    return ida_nalt.get_import_module_name(ordinal)


def idaapi_enum_import_names(ordinal: int, callback) -> None:
    import ida_nalt

    ida_nalt.enum_import_names(ordinal, callback)


main()
