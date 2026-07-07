from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        with self._lock:
            if not path.is_file():
                return default
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                corrupt = path.with_suffix(path.suffix + ".corrupt")
                path.replace(corrupt)
                return default

    def save(self, name: str, value: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

    def update_mapping(self, name: str, key: str, value: Any) -> None:
        data = self.load(name, {})
        data[key] = value
        self.save(name, data)

    def _path(self, name: str) -> Path:
        safe_name = name if name.endswith(".json") else f"{name}.json"
        return self.data_dir / safe_name

