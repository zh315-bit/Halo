"""AGENTS.md §11 / §3.9: core never imports shell, and core is stdlib-only."""

import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "halo_agent" / "core"
ALLOWED_THIRD_PARTY: set[str] = set()  # §3.9: YAML is parsed outside core and passed in as dicts


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")


def test_core_does_not_import_shell_or_third_party():
    for f in CORE.rglob("*.py"):
        for mod in _imports(f):
            assert "shell" not in mod.split("."), f"{f.name} imports {mod}"
            top = mod.lstrip(".").split(".")[0]
            if mod.startswith(".") or top in ("", "halo_agent"):
                continue
            assert top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY, f"{f.name} imports {mod}"
