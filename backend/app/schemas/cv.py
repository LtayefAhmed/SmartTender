"""CV read model."""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = ["CVRead"]


class CVRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    source: str
    folder: str | None = None
    display_name: str | None = None
    headline: str | None = None
    identity_source: str | None = None
    source_url: str | None = None
    uploaded_by: str | None = None
    created_at: datetime

    #: Set only on an import response, when the same bytes were already held.
    #: Not a column — the row returned in that case is the *existing* one, and
    #: this is how the caller tells "stored just now" from "already there".
    #: A bulk folder import re-run must read as "40 nouveaux, 460 déjà
    #: présents", not as 500 imports.
    is_duplicate: bool = False
