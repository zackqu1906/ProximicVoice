from __future__ import annotations

from collections import deque
from typing import Callable, Iterable

import numpy as np

from ..events import DetectionEvent, Stage1Event, Stage2Event
from .session_sink import ensure_session_sink


class ProximitySessionController:
    """Build whole near-speech utterances from the existing ProxiMic detector events.

    No VAD, RMS endpoint, second proximity model, or changed model input is used.

    Session semantics:
      * first Stage2 ACTIVATE -> START
      * an optional manual hold can also START/keep alive the same session
      * later Stage2 ACTIVATE -> keep the same session alive
      * N consecutive Stage2 rejects -> END
      * if speech truly stops, Stage1 also stops and therefore no reject can be
        produced; a Stage1-inactivity timeout is the necessary fallback END
      * max duration is a final safety bound

    The 16 kHz waveform is kept separate from ProxiMic's internal 8 kHz
    feature path. A completed utterance is submitted to a generic sink; the
    controller does not know whether that sink is ASR, a WAV saver, or a
    benchmark harness.
    """

    sample_rate = 16_000

    def __init__(
        self,
        sink,
        *,
        pre_roll_s: float = 1.5,
        end_rejects: int = 2,
        stage1_inactivity_s: float = 1.25,
        stage2_delay_s: float = 0.50,
        min_utterance_s: float = 0.40,
        max_utterance_s: float = 15.0,
        on_state: Callable[[str], None] | None = None,
        manual_active: Callable[[], bool] | None = None,
    ) -> None:
        if pre_roll_s < 0:
            raise ValueError("pre_roll_s cannot be negative")
        if end_rejects <= 0:
            raise ValueError("end_rejects must be positive")
        if stage1_inactivity_s <= 0:
            raise ValueError("stage1_inactivity_s must be positive")
        if stage2_delay_s < 0:
            raise ValueError("stage2_delay_s cannot be negative")
        if min_utterance_s <= 0 or max_utterance_s <= 0:
            raise ValueError("Utterance durations must be positive")
        if min_utterance_s > max_utterance_s:
            raise ValueError("min_utterance_s cannot exceed max_utterance_s")
        # The fallback must not fire while a Stage2 result from the latest
        # Stage1 trigger can still legitimately be pending.
        if stage1_inactivity_s <= stage2_delay_s:
            raise ValueError("stage1_inactivity_s must be greater than stage2_delay_s")

        self.sink = ensure_session_sink(sink)
        self.pre_roll_samples = int(round(pre_roll_s * self.sample_rate))
        self.end_rejects = int(end_rejects)
        self.stage1_inactivity_samples = int(round(stage1_inactivity_s * self.sample_rate))
        self.stage2_delay_samples = int(round(stage2_delay_s * self.sample_rate))
        self.min_utterance_samples = int(round(min_utterance_s * self.sample_rate))
        self.max_utterance_samples = int(round(max_utterance_s * self.sample_rate))
        self.on_state = on_state
        self.manual_active = manual_active

        # Rolling history is maintained at all times, including during ACTIVE,
        # so a second command can start immediately after the previous one.
        self._history: deque[np.ndarray] = deque()
        self._history_count = 0
        self._stream_samples = 0

        self._active = False
        self._utterance: list[np.ndarray] = []
        self._utterance_samples = 0
        self._session_start_sample = 0
        self._last_stage1_sample: int | None = None
        self._last_activate_sample: int | None = None
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample: int | None = None
        self._manual_was_active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def consecutive_rejects(self) -> int:
        return self._consecutive_rejects

    def process(self, block: np.ndarray, events: Iterable[DetectionEvent]) -> None:
        x = np.asarray(block, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        if not np.all(np.isfinite(x)):
            raise ValueError("ASR controller audio contains NaN or infinity")

        # run_source uses the same block for detector.feed() and for us.  Keep
        # an absolute sample clock matching Stage1Event/Stage2Event.sample_index.
        self._stream_samples += x.size
        self._append_history(x)

        if self._active:
            self._append_active(x)

        manual_now = bool(self.manual_active()) if self.manual_active is not None else False
        if manual_now and not self._manual_was_active and not self._active:
            self._begin_manual(x)
        self._manual_was_active = manual_now

        for event in events:
            if isinstance(event, Stage1Event):
                if self._active:
                    self._last_stage1_sample = event.sample_index
                continue

            if not isinstance(event, Stage2Event):
                continue

            if event.activated:
                if not self._active:
                    self._begin_from_history(event)
                else:
                    # A repeated ACTIVATE is not a new ASR command.  It is the
                    # heartbeat that confirms the same near-speaker session.
                    self._last_activate_sample = event.sample_index
                    self._consecutive_rejects = 0
                    self._first_reject_cutoff_sample = None
                continue

            # Rejects before the first ACTIVATE do not belong to an ASR session.
            if self._active:
                if manual_now:
                    # Explicit user control outranks automatic reject-based
                    # endpointing.  ACTIVATE/Stage1 evidence above is still
                    # observed so release can return cleanly to auto control.
                    continue
                self._handle_reject(event)
                if not self._active:
                    # END may have been reached on this event.
                    break

        if not self._active:
            return

        if self._utterance_samples >= self.max_utterance_samples:
            self._finish(reason="max-duration")
            return

        # If the user actually becomes quiet, Stage1 will stop firing and there
        # will be no Stage2 reject at all.  Use detector inactivity (not audio
        # RMS) to avoid a session that can never terminate.
        if (
            not manual_now
            and
            self._last_stage1_sample is not None
            and self._stream_samples - self._last_stage1_sample >= self.stage1_inactivity_samples
            and self._utterance_samples >= self.min_utterance_samples
        ):
            self._finish(reason="stage1-inactivity")

    def flush(self) -> None:
        """Submit an active utterance at EOF/shutdown, if it is long enough."""
        if self._active:
            self._finish(reason="flush")

    def abort(self) -> None:
        """Discard the active utterance without submitting a final result."""
        if self._active:
            self._log("[ASR] ABORT reason=device-disconnect")
        self._history.clear()
        self._history_count = 0
        self._active = False
        self._utterance = []
        self._utterance_samples = 0
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample = None
        abort_sink = getattr(self.sink, "abort", None)
        if callable(abort_sink):
            abort_sink()

    def reset(self) -> None:
        """Finish the current utterance and restart the detector-aligned clock.

        This is used when recognition is paused while the audio device remains
        connected.  Dropping the old rolling history prevents audio captured
        during the pause from becoming pre-roll after recognition resumes.
        """
        self.flush()
        self._history.clear()
        self._history_count = 0
        self._stream_samples = 0
        self._active = False
        self._utterance = []
        self._utterance_samples = 0
        self._session_start_sample = 0
        self._last_stage1_sample = None
        self._last_activate_sample = None
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample = None
        self._manual_was_active = False

    def close(self) -> None:
        self.flush()
        self.sink.close()

    def _append_history(self, x: np.ndarray) -> None:
        if self.pre_roll_samples <= 0:
            return
        block = x.copy()
        self._history.append(block)
        self._history_count += block.size

        while self._history and self._history_count - self._history[0].size >= self.pre_roll_samples:
            old = self._history.popleft()
            self._history_count -= old.size

        excess = self._history_count - self.pre_roll_samples
        if excess > 0 and self._history:
            first = self._history.popleft()
            trimmed = first[excess:].copy()
            self._history.appendleft(trimmed)
            self._history_count -= excess

    def _begin_from_history(self, event: Stage2Event) -> None:
        self._active = True
        self._utterance = [b.copy() for b in self._history]
        self._utterance_samples = sum(b.size for b in self._utterance)
        self._session_start_sample = self._stream_samples - self._utterance_samples

        # The Stage1 trigger that led to this ACTIVATE happened stage2_delay
        # earlier.  Seed the inactivity heartbeat from that trigger.
        inferred_stage1 = max(0, event.sample_index - self.stage2_delay_samples)
        self._last_stage1_sample = inferred_stage1
        self._last_activate_sample = event.sample_index
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample = None
        self._log(
            f"[ASR] START t={event.time_s:.3f}s "
            f"(pre-roll={self._utterance_samples / self.sample_rate:.2f}s)"
        )
        initial = (
            np.concatenate(self._utterance).astype(np.float32, copy=False)
            if self._utterance
            else np.empty(0, dtype=np.float32)
        )
        self.sink.start(initial)

    def _begin_manual(self, current_block: np.ndarray) -> None:
        self._active = True
        # Automatic activation needs history to compensate for the detector's
        # Stage2 delay.  A key press is immediate, so including that same
        # pre-roll could capture unrelated speech from before the user pressed
        # the shortcut.  Start from only the current (normally 20 ms) block.
        block = np.asarray(current_block, dtype=np.float32).reshape(-1).copy()
        self._utterance = [block]
        self._utterance_samples = block.size
        self._session_start_sample = self._stream_samples - block.size
        # If the detector sees real proximity evidence during the hold it will
        # update this heartbeat.  Otherwise release naturally reaches the
        # existing inactivity endpoint and closes the manually-started session.
        self._last_stage1_sample = self._stream_samples
        self._last_activate_sample = None
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample = None
        self._log(
            f"[ASR] START manual t={self._stream_samples / self.sample_rate:.3f}s "
            f"(lead-in={self._utterance_samples / self.sample_rate:.2f}s)"
        )
        self.sink.start(block)

    def _append_active(self, x: np.ndarray) -> None:
        block = x.copy()
        self._utterance.append(block)
        self._utterance_samples += block.size
        self.sink.feed(block)

    def _handle_reject(self, event: Stage2Event) -> None:
        self._consecutive_rejects += 1
        if self._consecutive_rejects == 1:
            # If multiple rejects are required to confirm END, do not send the
            # whole confirmation tail to ASR.  The first reject's Stage2 end is
            # a conservative cut point and avoids feeding ~0.5 s extra far
            # speech/noise from the final confirmation cycle.
            self._first_reject_cutoff_sample = event.sample_index

        if self._consecutive_rejects >= self.end_rejects:
            self._finish(
                reason=f"{self._consecutive_rejects}-rejects",
                cutoff_sample=self._first_reject_cutoff_sample,
            )

    def _finish(self, *, reason: str, cutoff_sample: int | None = None) -> None:
        if not self._active:
            return

        audio: np.ndarray | None = None
        if self._utterance:
            whole = np.concatenate(self._utterance).astype(np.float32, copy=False)
            if cutoff_sample is not None:
                keep = int(cutoff_sample - self._session_start_sample)
                keep = max(0, min(keep, whole.size))
                whole = whole[:keep]
            if whole.size >= self.min_utterance_samples:
                audio = whole

        duration_s = 0.0 if audio is None else audio.size / self.sample_rate
        self._log(
            f"[ASR] END reason={reason} duration={duration_s:.2f}s "
            f"rejects={self._consecutive_rejects}"
        )

        self.sink.end(
            audio if audio is not None else np.empty(0, dtype=np.float32)
        )

        # Keep rolling history intact; only the current session state resets.
        self._active = False
        self._utterance = []
        self._utterance_samples = 0
        self._session_start_sample = self._stream_samples
        self._last_stage1_sample = None
        self._last_activate_sample = None
        self._consecutive_rejects = 0
        self._first_reject_cutoff_sample = None

    def _log(self, message: str) -> None:
        if self.on_state is not None:
            self.on_state(message)


# Backward-compatible name used by older code/tests. New code should prefer
# ProximitySessionController because this class no longer depends on ASR.
ProximityASRController = ProximitySessionController


class DirectASRSessionController:
    """Continuously send source audio to ASR without running ProxiMic.

    This is a baseline/benchmark mode, not a replacement detector policy.  A
    session begins with the first 16 kHz input block, rolls over at a bounded
    duration, and is flushed on source EOF or Ctrl+C. The same generic
    ``SessionSink`` supports both streaming and completed-utterance ASRs.
    """

    sample_rate = 16_000

    def __init__(
        self,
        sink,
        *,
        session_duration_s: float = 15.0,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        if session_duration_s <= 0:
            raise ValueError("session_duration_s must be positive")
        self.sink = ensure_session_sink(sink)
        self.session_samples = int(round(session_duration_s * self.sample_rate))
        self.on_state = on_state
        self._parts: list[np.ndarray] = []
        self._samples = 0
        self._stream_samples = 0
        self._active = False

    def process(self, block: np.ndarray, _events: Iterable[DetectionEvent] = ()) -> None:
        x = np.asarray(block, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        if not np.all(np.isfinite(x)):
            raise ValueError("Direct ASR audio contains NaN or infinity")
        self._stream_samples += x.size

        if not self._active:
            self._active = True
            self._parts = [x.copy()]
            self._samples = x.size
            self._log(f"\n\n[ASR] START direct t={self._stream_samples / self.sample_rate:.3f}s")
            self.sink.start(x)
        else:
            block_copy = x.copy()
            self._parts.append(block_copy)
            self._samples += block_copy.size
            self.sink.feed(block_copy)

        if self._samples >= self.session_samples:
            self._finish(reason="direct-duration")

    def flush(self) -> None:
        if self._active:
            self._finish(reason="direct-flush")

    def abort(self) -> None:
        """Discard the active direct-ASR session without producing final text."""
        if self._active:
            self._log("[ASR] ABORT direct reason=device-disconnect")
        self._parts = []
        self._samples = 0
        self._active = False
        abort_sink = getattr(self.sink, "abort", None)
        if callable(abort_sink):
            abort_sink()

    def close(self) -> None:
        self.flush()
        self.sink.close()

    def _finish(self, *, reason: str) -> None:
        audio = np.concatenate(self._parts).astype(np.float32, copy=False)
        self._log(
            f"[ASR] END reason={reason} duration={audio.size / self.sample_rate:.2f}s\n"
        )
        self.sink.end(audio)
        self._parts = []
        self._samples = 0
        self._active = False

    def _log(self, message: str) -> None:
        if self.on_state is not None:
            self.on_state(message)
