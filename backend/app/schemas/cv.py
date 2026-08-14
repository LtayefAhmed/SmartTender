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
    source_url: str | None = None
    uploaded_by: str | None = None
    created_at: datetime
