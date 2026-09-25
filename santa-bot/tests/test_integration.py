"""The parts hang together: every button has a handler, every text is used, every module is reachable."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from app.core import texts
from app.handlers import callbacks, private, router  # noqa: F401  (router imports admin: its registrations)
from app.handlers.views import Action

ROOT = Path(__file__).resolve().parent.parent
TEXTS = ROOT / "app" / "core" / "texts.py"


def test_every_callback_action_has_exactly_one_handler() -> None:
    assert set(callbacks._HANDLERS) == {action.value for action in Action}


def test_admin_commands_are_registered() -> None:
    assert {"/stats", "/game", "/grant", "/refund", "/price", "/block", "/unblock", "/maintenance", "/admin"} <= {
        name for name, entry in private._COMMANDS.items() if entry.admin_only
    }
    assert not private._COMMANDS["/whoami"].admin_only


def test_every_text_is_used_outside_texts_py() -> None:
    defined = []
    for node in ast.parse(TEXTS.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef):
            defined.append(node.name)
        elif isinstance(node, ast.Assign):
            defined.extend(target.id for target in node.targets if isinstance(target, ast.Name))
    code = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in ("app", "tools")
        for path in (ROOT / folder).rglob("*.py")
        if path != TEXTS
    )
    helpers = {"MAX_MESSAGE", "fit_lines", "date_text", "names_preview", "REDRAW_PREFIX", "STATUS_LABELS"}
    unused = [name for name in defined if not name.startswith("_") and name not in helpers
              and not re.search(rf"\btexts\.{name}\b", code)]
    assert unused == []
    assert all(hasattr(texts, name) for name in helpers)


def test_every_app_module_is_imported_by_the_running_app() -> None:
    modules = sorted(
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "__init__.py"
    )
    probe = "import sys, app.main; print('\\n'.join(sorted(sys.modules)))"
    loaded = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
    assert set(modules) - set(loaded.stdout.split()) == set()
