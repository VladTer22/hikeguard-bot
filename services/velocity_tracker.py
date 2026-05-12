"""Per-chat sliding window of recent join requests.

Triggers 'raid mode' when burst exceeds threshold. Raid mode lasts
for raid_minutes after the last triggering event. Independent state
per chat_id. No I/O — caller injects timestamps for testability;
production callers should pass `time.monotonic()`.
"""

from collections import defaultdict, deque


class VelocityTracker:
    def __init__(self, threshold: int, window_sec: int, raid_minutes: int) -> None:
        self.threshold = threshold
        self.window_sec = window_sec
        self.raid_duration_sec = raid_minutes * 60
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._raid_until: dict[int, float] = {}

    def record(self, *, chat_id: int, ts: float) -> None:
        """Append a join event at the given timestamp."""
        self._events[chat_id].append(ts)

    def in_raid_mode(self, *, chat_id: int, now: float) -> bool:
        """Return True if chat is currently in raid mode at `now`.

        Side effects: evicts events outside the window; if the in-window
        count crosses the threshold, (re)sets the raid_until timestamp
        to `now + raid_duration_sec`.
        """
        events = self._events[chat_id]
        cutoff = now - self.window_sec
        # Window is half-open (now - window_sec, now]: an event at exactly
        # the cutoff is retained.
        while events and events[0] < cutoff:
            events.popleft()

        if len(events) >= self.threshold:
            self._raid_until[chat_id] = now + self.raid_duration_sec

        raid_until = self._raid_until.get(chat_id, 0)
        return now < raid_until
