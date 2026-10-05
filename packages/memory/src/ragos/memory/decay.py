"""Decay: how long a fact stays usable.

The rule this implements (AGENTS.md, memory rules 6 and 12): ``decay_score`` is
derived from **access recency over the validity window**, not maintained as an
independent counter. A counter drifts — it can only go up, it survives a fact that
stopped being true, and it cannot distinguish "read often, long ago" from "read
often, recently".

Two independent terms, because there are two different reasons to forget:

- **validity.** Outside ``[valid_from, valid_until]`` the fact is not a fact. This
  dominates: the score is zero no matter how often it was accessed. Access cannot
  resurrect a statement that has stopped being true.
- **recency.** Within its window, a fact read recently is worth more than one read
  long ago, decaying exponentially on a recorded half-life.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: Half-life of an unaccessed but still-valid fact. A recorded parameter rather
#: than a constant in a formula, so the sweep cadence and the number agree.
DEFAULT_HALF_LIFE_DAYS = 180.0

#: Below this a fact is no longer worth injecting into a prompt. Also recorded:
#: injecting a fact the user has effectively retracted produces confidently wrong
#: answers, which is worse than injecting nothing.
DEFAULT_ARCHIVE_THRESHOLD = 0.05

SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class DecayPolicy:
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    archive_threshold: float = DEFAULT_ARCHIVE_THRESHOLD

    def validate(self) -> None:
        if self.half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if not 0.0 < self.archive_threshold <= 1.0:
            raise ValueError("archive_threshold must be within (0, 1]")


def is_expired(*, valid_until: datetime | None, now: datetime) -> bool:
    return valid_until is not None and valid_until <= now


def decay_score(
    *,
    valid_from: datetime,
    valid_until: datetime | None,
    ingested_at: datetime,
    last_accessed_at: datetime | None,
    now: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Score a fact in ``[0, 1]``.

    Zero outside its validity window. Inside the window, exponential decay on the
    time since the fact was last either accessed or learned — whichever is more
    recent, because a fact that was just extracted has not decayed regardless of
    what some other copy of it has been doing.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if is_expired(valid_until=valid_until, now=now):
        return 0.0

    reference = max(
        filter(lambda value: value is not None, (last_accessed_at, ingested_at, valid_from)),
        default=now,
    )
    elapsed_days = max(0.0, (now - reference).total_seconds() / SECONDS_PER_DAY)
    return float(2.0 ** (-elapsed_days / half_life_days))


def archive_threshold(policy: DecayPolicy | None = None) -> float:
    return (policy or DecayPolicy()).archive_threshold


def should_archive(score: float, policy: DecayPolicy | None = None) -> bool:
    return score < archive_threshold(policy)


def days_until_expiry(
    *, valid_until: datetime | None, now: datetime
) -> float | None:
    """Remaining validity in days, or ``None`` when the fact does not expire."""
    if valid_until is None:
        return None
    return (valid_until - now).total_seconds() / SECONDS_PER_DAY
