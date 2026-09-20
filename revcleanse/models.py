"""Pydantic v2 schemas for the rev-cleanse pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RawLead(BaseModel):
    """One row exactly as it appears in the inbound CSV."""

    row_id: str
    first_name: str
    last_name: str
    email: str
    company_name: str
    website: str | None = None
    employee_count: int | None = None
    source: str
    timestamp: str

    @field_validator("website", "employee_count", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """CSV readers hand us '' where the business means 'unknown'."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("row_id", "first_name", "last_name", "company_name", "source", "timestamp", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CanonicalAccount(BaseModel):
    """A resolved company, assembled from one or more raw leads."""

    account_id: str
    canonical_domain: str | None = None
    normalized_name: str
    employee_count: int | None = None
    contact_ids: list[str] = Field(default_factory=list)
    source_row_ids: list[str] = Field(default_factory=list)
    last_updated_at: str


class CanonicalContact(BaseModel):
    """A resolved person, keyed on normalized email."""

    contact_id: str
    account_id: str
    first_name: str
    last_name: str
    email: str
    source_row_id: str


class MergeAuditEntry(BaseModel):
    """Why a row was folded into an account, and what it changed."""

    surviving_account_id: str
    source_row_id: str
    reason: str
    field_overrides: dict[str, str] = Field(default_factory=dict)
