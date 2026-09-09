"""One ceiling on a bounded pass: a count, and a clock that is RESERVED rather than checked.

Three passes in this repository bound themselves the same way, and they used to say so three times
in three byte-identical classes -- ``varsel._WriteBudget`` (the original, which wrote the wordings),
``notification_events._EmitBudget`` and ``webhook_delivery._DeliveryBudget``. This module is that one
implementation. Each caller keeps its own subclass docstring, because *why this pass has this
deadline* is different every time and is the part worth reading; what must not differ is the
arithmetic, whose failure is silent.

**The clock is reserved, not checked, and that is the whole point.** ``monotonic() >= deadline`` is
a pre-check: a call that passes it with a millisecond to spare still runs its full worst case
afterwards, so the pass overshoots by however long one unit of work can take -- measured at ~46 s
against a documented 30 s ceiling before ``varsel`` fixed it. Subtracting the worst case up front
turns the deadline into a ceiling on when the pass ENDS.

**Concurrency does not weaken it.** Every worker reserves its own worst case before starting, so
whatever is in flight when the deadline nears was already paid for. That is also why the lock is
here even for a single-threaded caller: it costs nothing, and one shape is easier to trust than two.

**Claim inside the worker as its task starts, never at submit time.** Submitting is what queues the
work; claimed at submit, every task is admitted before the deadline is consulted and the bound
guarantees nothing. That is ``/meldinger``'s recorded lesson, and it is the one line that is easy
to move and fatal to move.

``refused`` counts what the ceiling cost, so a pass can report it in its one summary line. A ceiling
nobody can see binding is not a ceiling anybody can size.
"""

from __future__ import annotations

import threading
from time import monotonic

__all__ = ["ReservedBudget"]


class ReservedBudget:
    """A count and a reserved wall clock behind one :meth:`claim`, claimable from several threads.

    Two limits behind one call, because a caller must not be able to satisfy one and ignore the
    other. ``worst_case_s`` is what a single unit of work can cost; it is reserved against
    ``deadline`` on every claim.
    """

    def __init__(self, max_calls: int, deadline: float, worst_case_s: float) -> None:
        self._lock = threading.Lock()
        self._max_calls = max_calls
        self._deadline = deadline
        self._worst_case = worst_case_s
        self.claimed = 0
        self.refused = 0

    def claim(self) -> bool:
        """Reserve one unit of work, or refuse. Never blocks beyond the arithmetic here."""
        with self._lock:
            if self.claimed >= self._max_calls or monotonic() + self._worst_case >= self._deadline:
                self.refused += 1
                return False
            self.claimed += 1
            return True
