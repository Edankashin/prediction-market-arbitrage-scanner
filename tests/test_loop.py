"""Tests for the cmd_loop() function in app/run.py.

All tests mock time.sleep (via stop.wait), time.monotonic, cmd_discover,
cmd_snapshot, cmd_kalshi_discover, and cmd_kalshi_snapshot so the loop runs
synchronously without I/O or real delays.

Design notes
------------
* cmd_loop accepts a `_stop` threading.Event that overrides the module-level
  flag.  Tests inject their own event to control termination.
* stop.wait(sleep_sec) is the only sleep in the loop.  With interval=0 the
  wall-clock adjusted sleep_sec is always 0.0, so the loop terminates after
  exactly the number of cycles controlled by the test.
* time.monotonic is patched via side_effect lists so each call returns a
  predictable value, enabling precise elapsed-time simulation.
* Kalshi functions are patched via the _mock_kalshi_cmds autouse fixture so
  existing tests need no changes; new Kalshi tests override as needed.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, call, patch

import pytest

from app.run import cmd_loop


# ---------------------------------------------------------------------------
# Autouse fixture: patch Kalshi commands for all tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_kalshi_cmds(monkeypatch):
    """Silently no-op Kalshi and cross-platform commands in all tests."""
    monkeypatch.setattr("app.run.cmd_kalshi_discover", lambda cfg: 0)
    monkeypatch.setattr("app.run.cmd_kalshi_snapshot", lambda cfg, top=None, by="volume_desc": 0)
    monkeypatch.setattr("app.run.cmd_cross_snapshot", lambda cfg: (0, 0))
    monkeypatch.setattr("app.run.cmd_cross_scan", lambda cfg: 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg() -> MagicMock:
    """Minimal fake config object."""
    cfg = MagicMock()
    cfg.db.path = ":memory:"
    return cfg


def _run_loop(
    *,
    monotonic_values: list[float],
    snapshot_side_effect,
    discover_side_effect=None,
    kalshi_discover_side_effect=None,
    kalshi_snapshot_side_effect=None,
    interval: int = 0,
    top: int = 10,
    by: str = "volume_24h",
    discover_every: int = 900,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Helper: patch all loop commands + monotonic, run cmd_loop, return mocks.

    Returns (mock_discover, mock_snapshot, mock_kalshi_discover, mock_kalshi_snapshot).
    """
    stop = threading.Event()
    cfg = _cfg()

    with (
        patch("app.run.cmd_discover", side_effect=discover_side_effect) as mock_discover,
        patch("app.run.cmd_snapshot", side_effect=snapshot_side_effect) as mock_snapshot,
        patch("app.run.cmd_kalshi_discover", side_effect=kalshi_discover_side_effect) as mock_k_discover,
        patch("app.run.cmd_kalshi_snapshot", side_effect=kalshi_snapshot_side_effect,
              return_value=0) as mock_k_snapshot,
        patch("time.monotonic", side_effect=monotonic_values),
    ):
        cmd_loop(cfg, interval=interval, top=top, by=by,
                 discover_every=discover_every, _stop=stop)

    return mock_discover, mock_snapshot, mock_k_discover, mock_k_snapshot


# ---------------------------------------------------------------------------
# Test 1: loop calls cmd_discover exactly once (the initial call) on start
# ---------------------------------------------------------------------------

class TestLoopDiscoversOnStart:
    def test_discover_called_once_before_loop(self) -> None:
        """cmd_discover is called exactly once (initial) when discover_every
        has not yet elapsed during the first snapshot cycle."""
        stop = threading.Event()

        snapshot_calls = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_calls[0] += 1
            stop.set()   # stop after the first snapshot cycle
            return 5

        cfg = _cfg()

        # monotonic: (1) last_discover_at init, (2) elapsed check on cycle 1
        # elapsed = 0.0 - 0.0 = 0.0 < 900 → no re-discover
        with (
            patch("app.run.cmd_discover") as mock_discover,
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        mock_discover.assert_called_once_with(cfg)
        assert snapshot_calls[0] == 1

    def test_discover_receives_cfg(self) -> None:
        """cmd_discover is called with the config object, not positional args."""
        stop = threading.Event()
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            stop.set()
            return 0

        with (
            patch("app.run.cmd_discover") as mock_discover,
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert mock_discover.call_args == call(cfg)


# ---------------------------------------------------------------------------
# Test 2: loop re-runs discover after discover_every seconds elapse
# ---------------------------------------------------------------------------

class TestLoopRediscoversAfterInterval:
    def test_rediscover_triggered_when_elapsed_exceeds_interval(self) -> None:
        """discover is called a second time when monotonic clock shows
        discover_every seconds have elapsed since the last call."""
        stop = threading.Event()
        snapshot_count = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 2:
                stop.set()
            return 3

        cfg = _cfg()

        # monotonic side_effect sequence:
        #   [0] → last_discover_at after initial discover   = 0.0
        #   [1] → elapsed check, cycle 1: 50.0 - 0.0 = 50  < 900 → no discover
        #   [2] → elapsed check, cycle 2: 901.0 - 0.0 = 901 ≥ 900 → discover!
        #   [3] → last_discover_at update after re-discover = 901.0
        with (
            patch("app.run.cmd_discover") as mock_discover,
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 50.0, 901.0, 901.0]),
        ):
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        # initial discover + one re-discover on cycle 2
        assert mock_discover.call_count == 2

    def test_rediscover_not_triggered_before_interval(self) -> None:
        """discover is NOT called a second time when elapsed < discover_every."""
        stop = threading.Event()
        snapshot_count = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 2:
                stop.set()
            return 3

        cfg = _cfg()

        # elapsed never reaches 900 → no re-discover
        with (
            patch("app.run.cmd_discover") as mock_discover,
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 100.0, 200.0]),
        ):
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        assert mock_discover.call_count == 1   # only the initial call

    def test_rediscover_resets_clock(self) -> None:
        """After re-discover fires, the elapsed clock resets so it doesn't
        trigger again immediately on the very next cycle."""
        stop = threading.Event()
        snapshot_count = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 3:
                stop.set()
            return 2

        cfg = _cfg()

        # cycle 1: elapsed=901 → re-discover, last_discover_at=901
        # cycle 2: elapsed=901-901=0 → no re-discover
        # cycle 3: stop
        with (
            patch("app.run.cmd_discover") as mock_discover,
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 901.0, 901.0, 901.0, 905.0]),
        ):
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        # initial + one re-discover (not two)
        assert mock_discover.call_count == 2


# ---------------------------------------------------------------------------
# Test 3: exceptions in cmd_snapshot don't crash the loop
# ---------------------------------------------------------------------------

class TestLoopContinuesAfterError:
    def test_snapshot_exception_does_not_crash_loop(self) -> None:
        """RuntimeError in cmd_snapshot is caught; loop continues to next cycle."""
        stop = threading.Event()
        snapshot_count = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] == 1:
                raise RuntimeError("network timeout")
            stop.set()   # second call stops the loop
            return 7

        cfg = _cfg()

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            # must not raise
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        assert snapshot_count[0] == 2, (
            "snapshot should be called twice: once failing, once succeeding"
        )

    def test_discover_exception_does_not_crash_loop(self) -> None:
        """Exception in the re-discover call is caught; loop continues."""
        stop = threading.Event()
        discover_count = [0]
        snapshot_count = [0]

        def fake_discover(cfg):
            discover_count[0] += 1
            if discover_count[0] == 2:   # re-discover (not initial) raises
                raise ConnectionError("gamma api unreachable")

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 2:
                stop.set()
            return 0

        cfg = _cfg()

        # cycle 1: elapsed=901 → re-discover raises; snapshot still runs (error caught)
        # cycle 2: stop
        with (
            patch("app.run.cmd_discover", side_effect=fake_discover),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 901.0, 901.0, 905.0]),
        ):
            cmd_loop(cfg, interval=0, top=10, by="volume_24h",
                     discover_every=900, _stop=stop)

        # Even though re-discover raised, the loop continued and snapshot ran
        assert snapshot_count[0] >= 1

    def test_multiple_consecutive_errors_dont_crash(self) -> None:
        """Three consecutive snapshot errors followed by success — loop survives."""
        stop = threading.Event()
        snapshot_count = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] < 4:
                raise ValueError(f"error #{snapshot_count[0]}")
            stop.set()
            return 1

        cfg = _cfg()

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0] + [0.0] * 10),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert snapshot_count[0] == 4


# ---------------------------------------------------------------------------
# SIGTERM stop-flag test
# ---------------------------------------------------------------------------

class TestLoopStopFlag:
    def test_stop_event_exits_cleanly(self) -> None:
        """Setting the stop event from snapshot causes the loop to exit."""
        stop = threading.Event()
        exited = [False]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            stop.set()
            return 0

        cfg = _cfg()

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("time.monotonic", side_effect=[0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)
            exited[0] = True

        assert exited[0], "cmd_loop must return after stop event is set"


# ---------------------------------------------------------------------------
# Kalshi integration in the loop
# ---------------------------------------------------------------------------

class TestLoopKalshiIntegration:
    def test_kalshi_discover_called_on_start(self) -> None:
        """cmd_kalshi_discover is called once during loop startup."""
        stop = threading.Event()
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            stop.set()
            return 0

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover") as mock_k_discover,
            patch("app.run.cmd_kalshi_snapshot", return_value=0),
            patch("time.monotonic", side_effect=[0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        mock_k_discover.assert_called_once_with(cfg)

    def test_kalshi_snapshot_called_each_cycle(self) -> None:
        """cmd_kalshi_snapshot is called on every snapshot cycle."""
        stop = threading.Event()
        snapshot_count = [0]
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 2:
                stop.set()
            return 3

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover"),
            patch("app.run.cmd_kalshi_snapshot", return_value=5) as mock_k_snap,
            patch("time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert mock_k_snap.call_count == 2

    def test_kalshi_snapshot_error_does_not_crash_loop(self) -> None:
        """Exception in cmd_kalshi_snapshot is caught; loop continues."""
        stop = threading.Event()
        snap_count = [0]
        cfg = _cfg()

        def fake_kalshi_snapshot(c, top=None, by="volume_desc"):
            if snap_count[0] < 1:
                raise RuntimeError("kalshi api down")
            return 0

        def fake_snapshot(c, top=None, by="volume_24h"):
            snap_count[0] += 1
            if snap_count[0] >= 2:
                stop.set()
            return 1

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover"),
            patch("app.run.cmd_kalshi_snapshot", side_effect=fake_kalshi_snapshot),
            patch("time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert snap_count[0] == 2

    def test_kalshi_discover_called_on_rediscover(self) -> None:
        """cmd_kalshi_discover is called again when rediscover interval elapses."""
        stop = threading.Event()
        snap_count = [0]
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            snap_count[0] += 1
            if snap_count[0] >= 2:
                stop.set()
            return 0

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover") as mock_k_discover,
            patch("app.run.cmd_kalshi_snapshot", return_value=0),
            # [0] init, [1] cycle1 elapsed→901≥900 rediscover, [2] last_discover_at update,
            # [3] cycle2 elapsed
            patch("time.monotonic", side_effect=[0.0, 901.0, 901.0, 905.0]),
        ):
            cmd_loop(cfg, interval=0, discover_every=900, _stop=stop)

        # initial + one rediscover = 2 calls
        assert mock_k_discover.call_count == 2


# ---------------------------------------------------------------------------
# Cross-platform integration in the loop
# ---------------------------------------------------------------------------

class TestLoopCrossIntegration:
    def test_cross_snapshot_called_each_cycle(self) -> None:
        """cmd_cross_snapshot is called on every snapshot cycle."""
        stop = threading.Event()
        snapshot_count = [0]
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 3:
                stop.set()
            return 2

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover"),
            patch("app.run.cmd_kalshi_snapshot", return_value=0),
            patch("app.run.cmd_cross_snapshot", return_value=(2, 1)) as mock_x_snap,
            patch("app.run.cmd_cross_scan", return_value=0),
            patch("time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert mock_x_snap.call_count == 3

    def test_cross_scan_called_on_cycle_multiple_of_scan_every(self) -> None:
        """cmd_cross_scan is called every _SCAN_EVERY (5) cycles, not every cycle."""
        stop = threading.Event()
        snapshot_count = [0]
        cfg = _cfg()

        def fake_snapshot(c, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 5:
                stop.set()
            return 1

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover"),
            patch("app.run.cmd_kalshi_snapshot", return_value=0),
            patch("app.run.cmd_cross_snapshot", return_value=(0, 0)),
            patch("app.run.cmd_cross_scan", return_value=0) as mock_x_scan,
            patch("time.monotonic", side_effect=[0.0] + [0.0] * 10),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        # Only cycle 5 is a multiple of _SCAN_EVERY=5, so called exactly once
        assert mock_x_scan.call_count == 1

    def test_cross_snapshot_error_does_not_crash_loop(self) -> None:
        """Exception in cmd_cross_snapshot is caught; loop continues."""
        stop = threading.Event()
        snap_count = [0]
        cfg = _cfg()

        cs_call_count = [0]

        def fake_cross_snapshot(c):
            cs_call_count[0] += 1
            if cs_call_count[0] == 1:
                raise RuntimeError("clob api down")
            return (0, 0)

        def fake_snapshot(c, top=None, by="volume_24h"):
            snap_count[0] += 1
            if snap_count[0] >= 2:
                stop.set()
            return 1

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_kalshi_discover"),
            patch("app.run.cmd_kalshi_snapshot", return_value=0),
            patch("app.run.cmd_cross_snapshot", side_effect=fake_cross_snapshot),
            patch("app.run.cmd_cross_scan", return_value=0),
            patch("time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            cmd_loop(cfg, interval=0, _stop=stop)

        assert snap_count[0] == 2
