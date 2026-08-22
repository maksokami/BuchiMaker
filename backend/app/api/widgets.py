"""
API router: /widgets  –  widget-set (frontend rendering package) lifecycle.

Endpoints
---------
GET    /api/v1/widgets                     – list all registered widget sets
POST   /api/v1/widgets/load                – register/reload a widget set from a folder
POST   /api/v1/widgets/{widget_set_id}/activate – make this widget set the active one
DELETE /api/v1/widgets/{widget_set_id}     – unregister a widget set

Design
------
A widget set is a folder under ``WIDGETS_DIR`` containing an ``index.js`` that
exports a Vue component registry (see ``frontend/widgets/default/index.js``).
The backend only tracks registration and which set is active — it never
executes the JS. The frontend reads the active set's ID and dynamically
imports ``../widgets/<id>/index.js`` at runtime, so activating a set here
immediately changes which widget styles the dashboard views render with.

Only one widget set can be active at a time — activating one implicitly
deactivates whichever was active before, since "active" is a single global
setting rather than a per-set flag.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.core.logging import get_logger
from app.core.roles import ADMIN_ONLY, require_role
from app.core.validator import raise_if_invalid_name
from app.models.schemas import (
    WidgetSetInfo,
    WidgetSetLoadRequest,
    WidgetSetLoadResponse,
)
from app.models.system_manager import system_manager

router = APIRouter(
    prefix="/widgets",
    tags=["widgets"],
    # Widget sets change global rendering for every dashboard/user at once —
    # Administrator-only, not part of Data Admin's grant (dashboards/data
    # sources/DB SQL). Applied at router level since every route here needs
    # the same gate, unlike system.py/dashboards.py's mixed permissions.
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
_logger = get_logger("buchimaker.api.widgets")

_404_widget = {
    "description": "Widget set not found — not registered or wrong ID.",
    "content": {
        "application/json": {"example": {"detail": "Widget set 'custom_theme' not found."}}
    },
}
_422_folder = {
    "description": "Folder does not exist, has no index.js, or the folder name contains invalid characters.",
    "content": {
        "application/json": {
            "example": {"detail": "'index.js' not found in widget folder: /app/widgets/custom_theme"}
        }
    },
}


@router.get(
    "",
    response_model=List[WidgetSetInfo],
    summary="List all registered widget sets",
    description=(
        "Returns every widget set currently registered, each flagged with "
        "whether it is the one the frontend is actively rendering dashboards "
        "with. Exactly one set is active at any time."
    ),
)
async def list_widget_sets():
    """Return metadata for all registered widget sets.

    Returns:
        List of WidgetSetInfo objects, or an empty list.
    """
    return system_manager.list_widget_sets()


@router.post(
    "/load",
    response_model=WidgetSetLoadResponse,
    status_code=status.HTTP_200_OK,
    summary="Register or reload a widget set from a folder",
    description=(
        "Validates a widget package folder (must contain `index.js`) and "
        "registers it.\n\n"
        "**Hot-reload supported:** calling this endpoint again with the same "
        "folder re-validates it and refreshes its display title.\n\n"
        "**Folder path resolution:**\n"
        "- Absolute path: used as-is (`/app/widgets/custom_theme`).\n"
        "- Relative path: resolved against the `WIDGETS_DIR` env variable.\n\n"
        "The first widget set ever registered automatically becomes active."
    ),
    responses={422: _422_folder},
)
async def load_widget_set(body: WidgetSetLoadRequest):
    """Register or reload a widget set from a folder path.

    Args:
        body: Request containing the folder path.

    Returns:
        WidgetSetLoadResponse with status and total widget sets loaded count.

    Raises:
        HTTPException: 422 if the folder or its `index.js` is missing, or the
            folder name contains invalid characters.
    """
    error = system_manager.load_widget_set(body.folder_path)
    if error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error,
        )
    return WidgetSetLoadResponse(
        status="ok", widget_sets_loaded=len(system_manager.widget_sets)
    )


@router.post(
    "/{widget_set_id}/activate",
    response_model=WidgetSetInfo,
    summary="Make this widget set the active one",
    description=(
        "Marks the given widget set as the one dashboards render with. "
        "Since only one widget set is active at a time, this implicitly "
        "deactivates whichever set was active before."
    ),
    responses={404: _404_widget, 422: _404_widget},
)
async def activate_widget_set(
    widget_set_id: str = Path(description="Widget set ID. Example: `default`"),
):
    """Activate a registered widget set.

    Args:
        widget_set_id: The widget set ID to activate.

    Returns:
        WidgetSetInfo for the now-active widget set.

    Raises:
        HTTPException: 404 if the widget set is not registered.
        HTTPException: 422 if widget_set_id contains invalid characters.
    """
    raise_if_invalid_name(widget_set_id, "widget_set_id")
    if not system_manager.activate_widget_set(widget_set_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget set '{widget_set_id}' not found.",
        )
    ws = system_manager.widget_sets[widget_set_id]
    return WidgetSetInfo(**ws.to_info(active=True))


@router.delete(
    "/{widget_set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a widget set",
    description=(
        "Removes the widget set from the registry.\n\n"
        "The underlying folder on disk is **not** deleted — only the "
        "registration is removed.\n\n"
        "**The currently active widget set cannot be deleted** — activate a "
        "different one first.\n\n"
        "Returns **204 No Content** on success (no response body)."
    ),
    responses={
        204: {"description": "Widget set unregistered. No response body."},
        400: {
            "description": "Cannot delete the currently active widget set.",
            "content": {
                "application/json": {
                    "example": {"detail": "Cannot delete widget set 'default' while it is active. Activate a different widget set first."}
                }
            },
        },
        404: _404_widget,
        422: _404_widget,
    },
)
async def delete_widget_set(
    widget_set_id: str = Path(description="Widget set ID to remove. Example: `custom_theme`"),
):
    """Remove a registered widget set.

    Args:
        widget_set_id: The widget set ID to remove.

    Raises:
        HTTPException: 404 if the widget set is not registered.
        HTTPException: 422 if widget_set_id contains invalid characters.
        HTTPException: 400 if the widget set is currently active.
    """
    raise_if_invalid_name(widget_set_id, "widget_set_id")
    try:
        removed = system_manager.remove_widget_set(widget_set_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget set '{widget_set_id}' not found.",
        )
