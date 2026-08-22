"""
WidgetSet domain object — a registered frontend widget-rendering package.

A widget set is a folder (living under ``WIDGETS_DIR``, served statically by
the frontend container) containing an ``index.js`` that exports a Vue
component registry — see ``frontend/widgets/default/index.js`` for the
reference shape: ``export default { packageName, components: {...} }``.

The backend only tracks *registration* (the folder exists and has an
``index.js``) and which set is currently active; it never executes or
imports the JS itself — that happens client-side via a same-origin dynamic
``import('../widgets/<id>/index.js')`` in the frontend.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.validator import sanitize_name

# Best-effort extraction of `packageName: 'xxx'` from index.js for a nicer
# display title. Falls back to the folder name if not found or unreadable —
# this is cosmetic only, never a source of truth for the widget set's id.
_PACKAGE_NAME_RE = re.compile(r"packageName\s*:\s*['\"]([^'\"]+)['\"]")


class WidgetSet:
    """A registered widget package folder.

    Attributes:
        id: Sanitized folder name — also the URL segment the frontend
            dynamically imports as ``../widgets/<id>/index.js``.
        title: Display name — the ``packageName`` declared in ``index.js``
            if present, otherwise the same as ``id``.
        description: Always ``None`` today; no metadata source exists yet.
        folder_path: Resolved absolute path to the widget package folder.
        loaded_at: Epoch of the last successful load/reload.
    """

    def __init__(self):
        """Create an empty WidgetSet. Call :meth:`load` to populate."""
        self.id: str = ""
        self.title: str = ""
        self.description: Optional[str] = None
        self.folder_path: Optional[str] = None
        self.loaded_at: int = 0

    def load(self, folder_path: str) -> str:
        """Validate and (re)register a widget package folder.

        Args:
            folder_path: Absolute path to the widget package folder.

        Returns:
            Empty string on success, or a user-friendly error message.
        """
        path = Path(folder_path)
        if not path.is_dir():
            return f"Widget folder not found: {folder_path}"

        index_file = path / "index.js"
        if not index_file.exists():
            return f"'index.js' not found in widget folder: {folder_path}"

        widget_id = path.name
        try:
            sanitize_name(widget_id, "widget set id")
        except ValueError as exc:
            return f"Validation error: {exc}"

        title = widget_id
        try:
            match = _PACKAGE_NAME_RE.search(index_file.read_text(encoding="utf-8"))
            if match:
                title = match.group(1)
        except OSError:
            pass

        self.id = widget_id
        self.title = title
        self.folder_path = str(path)
        self.loaded_at = int(time.time())
        return ""

    def to_info(self, active: bool) -> Dict[str, Any]:
        """Serialise this widget set for API responses.

        Args:
            active: Whether this set is the currently active one.

        Returns:
            Dict compatible with the ``WidgetSetInfo`` schema.
        """
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "folder_path": self.folder_path,
            "active": active,
        }
