"""core/ stays platform-agnostic (§1): no aiohttp and nothing MAX-specific."""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "app" / "core"
FORBIDDEN = ("aiohttp", "app.max_api", "app.outbox", "app.context", "app.handlers", "app.web", "tools")


def test_core_does_not_import_platform_code() -> None:
    for path in CORE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(FORBIDDEN), f"{path.name} imports {name}"
