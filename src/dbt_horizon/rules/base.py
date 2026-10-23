"""Outcomes and findings shared by all rules."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    OK = "ok"                        # runs as is
    AUTO_FIXED = "auto_fixed"        # the overlay changes it deterministically
    REVIEW = "review"                # runs, but a person should check the result
    NEEDS_MAPPING = "needs_mapping"  # config input required (e.g. where the Iceberg copy lives)
    UNSUPPORTED = "unsupported"      # can't run on DuckDB/Horizon

    @property
    def rank(self) -> int:
        return list(Outcome).index(self)

    @property
    def blocking(self) -> bool:
        """The subject (and everything downstream) can't run."""
        return self in (Outcome.NEEDS_MAPPING, Outcome.UNSUPPORTED)


@dataclass(frozen=True)
class Finding:
    rule: str          # e.g. "sources.not_in_horizon"
    subject: str       # dbt unique_id, or "project"
    outcome: Outcome
    message: str
    action: str = ""   # what the overlay does, or what the user has to do

    def to_dict(self) -> dict:
        return {"rule": self.rule, "subject": self.subject, "outcome": self.outcome.value,
                "message": self.message, "action": self.action}
