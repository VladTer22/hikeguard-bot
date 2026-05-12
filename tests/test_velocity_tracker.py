from services.velocity_tracker import VelocityTracker


def test_below_threshold_does_not_trigger_raid() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(9):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is False


def test_threshold_reached_triggers_raid() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True


def test_raid_mode_expires_after_duration() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    # 20 minutes = 1200 seconds later
    assert tracker.in_raid_mode(chat_id=1, now=10 + 1201) is False


def test_old_events_outside_window_dont_count() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    # First check is at ts=10, raid mode triggers
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    # But if we never checked and only check 200s later with no new events,
    # the window has expired, raid mode was never set
    tracker2 = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker2.record(chat_id=1, ts=i)
    # At ts=200, window=60 → only events from ts>=140 count; we have none
    assert tracker2.in_raid_mode(chat_id=1, now=200) is False


def test_separate_chats_have_independent_state() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    assert tracker.in_raid_mode(chat_id=2, now=10) is False


def test_raid_mode_extends_on_continued_burst() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=10)  # triggers raid_until = 10 + 1200 = 1210
    # 19 minutes in (still raid), another burst extends the timer
    for i in range(1140, 1150):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=1150)  # extends to 1150 + 1200
    # At 1300 (well past original expiry of 1210), still in raid mode
    assert tracker.in_raid_mode(chat_id=1, now=1300) is True


def test_window_eviction_keeps_memory_bounded() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    # Hammer with 10000 old events, none should be retained at t=10000
    for i in range(10000):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=10000)
    # window_sec=60, events at ts=0..9999, now=10000 → only ts >= 9940 retained
    assert len(tracker._events[1]) <= 61


def test_event_at_exact_cutoff_is_retained() -> None:
    """Window is half-open (now - window_sec, now]; event at exact cutoff stays in."""
    tracker = VelocityTracker(threshold=2, window_sec=60, raid_minutes=20)
    # Event at ts=0; check at now=60. Cutoff = 60 - 60 = 0.
    # Eviction condition is events[0] < cutoff → 0 < 0 is False, so event stays.
    tracker.record(chat_id=1, ts=0)
    tracker.record(chat_id=1, ts=60)
    # Two events in window (boundary-inclusive) meets threshold=2 → raid triggers
    assert tracker.in_raid_mode(chat_id=1, now=60) is True
