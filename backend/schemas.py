"""Pydantic response models for the Phase 6 API."""
from typing import Any
from pydantic import BaseModel
class HealthResponse(BaseModel):
    status: str
    database: str
class DataResponse(BaseModel):
    data: Any
