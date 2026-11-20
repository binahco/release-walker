from __future__ import annotations

from pydantic import BaseModel


class ReleaseNotes(BaseModel):
    """Notas de release con veredicto del gate (`release-notes-v1`)."""

    version: str
    ready: bool
    blockers: list[str] = []
    notes: str
