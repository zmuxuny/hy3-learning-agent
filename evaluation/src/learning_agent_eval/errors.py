"""Structured, safely renderable validation outcomes."""

from __future__ import annotations

from dataclasses import dataclass

TRACK_ORDER = ("planning", "intervention", "assessment", "revision")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One data error without a copy of the rejected value."""

    code: str
    path: str
    message: str
    file: str = "<memory>"

    def sort_key(self) -> tuple[str, str, str, str]:
        """Return the repository-wide stable error ordering key."""

        return (self.file, self.path, self.code, self.message)

    def render(self) -> str:
        """Render metadata only; sensitive input values are never included."""

        return (
            f"ERROR file={self.file} path={self.path} "
            f"code={self.code} message={self.message}"
        )


@dataclass(frozen=True, slots=True)
class DatasetStats:
    """Validated Decision Episode counts in fixed track order."""

    episodes: int
    by_track: tuple[tuple[str, int], ...]

    @classmethod
    def from_counts(cls, counts: dict[str, int]) -> DatasetStats:
        return cls(
            episodes=sum(counts.get(track, 0) for track in TRACK_ORDER),
            by_track=tuple((track, counts.get(track, 0)) for track in TRACK_ORDER),
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Complete deterministic validation result."""

    issues: tuple[ValidationIssue, ...]
    stats: DatasetStats

    @property
    def ok(self) -> bool:
        return not self.issues
