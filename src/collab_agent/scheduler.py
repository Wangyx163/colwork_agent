"""Make time pass without anybody watching.

Every part of a reminder already existed. `evaluate_policy` decides which
tasks are overdue, which have gone quiet, and what to send about them;
`advance_time` moves the meeting's clock; the Outbox delivers. What was missing
is the thing that runs them -- `evaluate_policy` was called from the evaluation
harness and from nowhere in the serving path, so a meeting only ever noticed a
deadline if somebody happened to run the deterministic scenario.

That gap is the difference between a task list and a system that follows up. A
tracker that only acts while you are looking at it has not taken anything off
your plate.

## Why the clock is advanced here rather than left to the domain

`now()` reads `episodes.current_sim_time`, and it moves only when something
explicitly advances it. That is deliberate: the deterministic evaluation
depends on a clock that does not drift, or a scenario would produce different
results on a slow machine. So the *serving* runtime is where wall time enters,
and it enters in one place -- this loop -- rather than by making `now()`
read the system clock, which would make every test time-dependent.

## Why it does not send anything itself

It advances the clock and asks the domain what that implies. Everything it
produces goes into the Outbox like every other effect, and the same dispatcher
delivers it under the same EffectId idempotency. A scheduler with its own
delivery path would be a second way for a message to leave the building, and
the first thing that would rot is which of the two had already sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


def wall_clock_iso(timezone_name: str = "Australia/Sydney") -> str:
    """Now, in the timezone the meeting keeps its deadlines in.

    Not UTC: a deadline of "Friday" means Friday where the team is, and a
    reminder that fires on Thursday evening local time because the comparison
    happened in UTC is worse than no reminder -- it teaches people the dates
    are wrong.
    """

    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return datetime.now(ZoneInfo(timezone_name)).isoformat()
    except Exception:  # noqa: BLE001 - an unknown zone must not stop the clock
        return datetime.now(timezone.utc).isoformat()


class Scheduler:
    """One meeting's clock and the reminders that follow from it."""

    def __init__(
        self,
        service: Any,
        *,
        timezone_name: str = "Australia/Sydney",
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        self.timezone_name = timezone_name
        self.log = log or (lambda line: None)

    def tick(self) -> dict[str, Any]:
        """Advance to wall time and act on whatever that makes true.

        Returns what happened rather than logging and swallowing it, so a
        caller -- a test, or an operator page -- can see that a tick did
        nothing without having to read a log.
        """

        target = wall_clock_iso(self.timezone_name)
        before = self.service.now()
        advanced = False
        try:
            # Refuses to go backwards, which is what a clock correction on the
            # host would otherwise do to a meeting mid-flight.
            if target > before:
                self.service.advance_time(target)
                advanced = True
        except Exception as error:  # noqa: BLE001 - a tick must survive
            self.log(f"[scheduler] clock advance failed: {error!r}")

        decisions: list[dict[str, Any]] = []
        try:
            decisions = self.service.evaluate_policy()
        except Exception as error:  # noqa: BLE001 - a tick must survive
            self.log(f"[scheduler] policy evaluation failed: {error!r}")

        if decisions:
            # `level`, not `action_type` -- a decision reports which escalation
            # tier it opened, and the first version of this line printed "?"
            # for every one of them. A log that cannot name what it saw is a
            # log that gets ignored.
            sent = [one for one in decisions if not one.get("suppressed")]
            held = len(decisions) - len(sent)
            levels = ", ".join(
                sorted({str(one.get("level") or "?") for one in sent})
            ) or "无"
            self.log(
                f"[scheduler] {self.service.episode_id[-12:]}: "
                f"{len(sent)} 条到期动作（{levels}）"
                + (f"，另有 {held} 条因当日触达上限被压下" if held else "")
            )
        return {
            "episode_id": self.service.episode_id,
            "advanced": advanced,
            "from": before,
            "to": self.service.now(),
            "decisions": decisions,
        }


def run_forever(
    schedulers: list[Scheduler],
    *,
    stop: Any,
    interval_seconds: float,
    log: Callable[[str], None] | None = None,
) -> None:
    """Tick every meeting, forever, on the caller's thread.

    Each meeting is ticked independently and one failing does not stop the
    next: a scheduler that dies on the first meeting with bad data would
    silently stop following up on every meeting behind it.
    """

    say = log or (lambda line: None)
    say(f"[scheduler] 开始，每 {interval_seconds:.0f} 秒检查一次到期")
    while not stop.wait(interval_seconds):
        for scheduler in schedulers:
            try:
                scheduler.tick()
            except Exception as error:  # noqa: BLE001 - the loop must survive
                say(f"[scheduler] tick failed: {error!r}")
