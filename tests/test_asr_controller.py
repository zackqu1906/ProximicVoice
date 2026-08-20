import numpy as np

from proximic_ring.asr.controller import DirectASRSessionController, ProximityASRController
from proximic_ring.events import Stage1Event, Stage2Event


class ImmediateWorker:
    def __init__(self):
        self.items = []
        self.closed = False

    def submit(self, audio):
        self.items.append(np.asarray(audio).copy())

    def close(self):
        self.closed = True


def activate_event(sample_index: int):
    t = sample_index / 16_000
    return Stage2Event(
        sample_index=sample_index,
        time_s=t,
        window_start_s=t - 1.0,
        window_end_s=t,
        score=2.0,
        logits=(2.0, 0.0),
        activated=True,
    )


def reject_event(sample_index: int):
    t = sample_index / 16_000
    return Stage2Event(
        sample_index=sample_index,
        time_s=t,
        window_start_s=t - 1.0,
        window_end_s=t,
        score=-1.0,
        logits=(0.0, 1.0),
        activated=False,
    )


def stage1_event(sample_index: int):
    t = sample_index / 16_000
    return Stage1Event(sample_index=sample_index, time_s=t, max_amplitude=0.5)


def make_gate(worker, **kwargs):
    defaults = dict(
        pre_roll_s=0.04,
        end_rejects=2,
        stage1_inactivity_s=0.08,
        stage2_delay_s=0.02,
        min_utterance_s=0.02,
        max_utterance_s=2.0,
    )
    defaults.update(kwargs)
    return ProximityASRController(worker, **defaults)


def test_no_activation_never_submits():
    worker = ImmediateWorker()
    gate = make_gate(worker)
    block = np.ones(320, dtype=np.float32) * 0.1
    gate.process(block, [reject_event(320)])
    gate.process(block, [])
    gate.flush()
    assert worker.items == []


def test_first_activate_starts_repeated_activate_keeps_session_and_two_rejects_end():
    worker = ImmediateWorker()
    gate = make_gate(worker, stage1_inactivity_s=1.0)

    a = np.full(320, 0.10, dtype=np.float32)
    b = np.full(320, 0.20, dtype=np.float32)
    c = np.full(320, 0.30, dtype=np.float32)
    d = np.full(320, 0.40, dtype=np.float32)
    e = np.full(320, 0.50, dtype=np.float32)

    gate.process(a, [])
    gate.process(b, [activate_event(640)])
    assert gate.active

    gate.process(c, [activate_event(960)])
    assert gate.active
    assert gate.consecutive_rejects == 0

    gate.process(d, [reject_event(1280)])
    assert gate.active
    assert gate.consecutive_rejects == 1

    gate.process(e, [reject_event(1600)])
    assert not gate.active
    assert len(worker.items) == 1

    # The second reject is confirmation only.  Audio is cut at the first
    # reject's Stage2 end (sample 1280), so the final confirmation block is not
    # sent to ASR.
    out = worker.items[0]
    assert out.size == 4 * 320
    np.testing.assert_allclose(out[:320], a)
    np.testing.assert_allclose(out[320:640], b)


def test_activate_after_one_reject_cancels_pending_end():
    worker = ImmediateWorker()
    gate = make_gate(worker, stage1_inactivity_s=1.0)
    block = np.full(320, 0.2, dtype=np.float32)

    gate.process(block, [activate_event(320)])
    gate.process(block, [reject_event(640)])
    assert gate.consecutive_rejects == 1

    gate.process(block, [activate_event(960)])
    assert gate.consecutive_rejects == 0
    assert gate.active

    gate.process(block, [reject_event(1280)])
    gate.process(block, [reject_event(1600)])
    assert not gate.active
    assert len(worker.items) == 1


def test_stage1_inactivity_ends_when_silence_produces_no_reject():
    worker = ImmediateWorker()
    gate = make_gate(
        worker,
        pre_roll_s=0.02,
        stage1_inactivity_s=0.06,
        stage2_delay_s=0.02,
    )
    block = np.full(320, 0.2, dtype=np.float32)

    # ACTIVATE at sample 640 implies its Stage1 trigger was around sample 320.
    gate.process(block, [])
    gate.process(block, [activate_event(640)])
    assert gate.active

    gate.process(block, [])       # sample 960: 0.04 s since inferred Stage1
    gate.process(block, [])       # sample 1280: 0.06 s -> inactivity END
    assert not gate.active
    assert len(worker.items) == 1


def test_new_stage1_heartbeat_postpones_inactivity_end():
    worker = ImmediateWorker()
    gate = make_gate(
        worker,
        pre_roll_s=0.02,
        stage1_inactivity_s=0.06,
        stage2_delay_s=0.02,
    )
    block = np.full(320, 0.2, dtype=np.float32)

    gate.process(block, [])
    gate.process(block, [activate_event(640)])
    gate.process(block, [stage1_event(960)])
    gate.process(block, [])  # only 0.02 s since Stage1
    assert gate.active

    gate.process(block, [])  # 0.04 s
    gate.process(block, [])  # 0.06 s -> END
    assert not gate.active
    assert len(worker.items) == 1


def test_flush_submits_active_utterance():
    worker = ImmediateWorker()
    gate = make_gate(worker, pre_roll_s=0.02, stage1_inactivity_s=1.0)
    block = np.full(320, 0.2, dtype=np.float32)
    gate.process(block, [activate_event(320)])
    gate.flush()
    assert len(worker.items) == 1


def test_abort_discards_active_utterance_on_device_disconnect():
    worker = ImmediateWorker()
    gate = make_gate(worker, pre_roll_s=0.02, stage1_inactivity_s=1.0)
    block = np.full(320, 0.2, dtype=np.float32)
    gate.process(block, [activate_event(320)])

    gate.abort()
    gate.close()

    assert not gate.active
    assert worker.items == []
    assert worker.closed


def test_pause_reset_flushes_and_discards_old_pre_roll_and_sample_clock():
    worker = ImmediateWorker()
    gate = make_gate(worker, pre_roll_s=0.02, stage1_inactivity_s=1.0)
    old = np.full(320, 0.2, dtype=np.float32)
    new = np.full(320, 0.8, dtype=np.float32)

    gate.process(old, [activate_event(320)])
    gate.reset()
    assert not gate.active
    assert len(worker.items) == 1

    # Detection events start again at sample 320 after detector.reset().
    gate.process(new, [activate_event(320)])
    gate.flush()
    assert len(worker.items) == 2
    np.testing.assert_allclose(worker.items[1], new)


def test_manual_hold_starts_without_activation_and_overrides_inactivity():
    worker = ImmediateWorker()
    manual = [False]
    gate = make_gate(worker, manual_active=lambda: manual[0])
    block = np.full(320, 0.2, dtype=np.float32)

    manual[0] = True
    gate.process(block, [])
    assert gate.active
    for _ in range(5):
        gate.process(block, [])
    assert gate.active

    manual[0] = False
    gate.process(block, [])
    assert not gate.active
    assert len(worker.items) == 1


def test_manual_start_does_not_include_automatic_pre_roll():
    worker = ImmediateWorker()
    manual = [False]
    gate = make_gate(
        worker,
        manual_active=lambda: manual[0],
        pre_roll_s=0.04,
    )
    before_press = np.full(320, 0.1, dtype=np.float32)
    after_press = np.full(320, 0.8, dtype=np.float32)

    gate.process(before_press, [])
    manual[0] = True
    gate.process(after_press, [])
    manual[0] = False
    for _ in range(4):
        gate.process(after_press, [])

    assert len(worker.items) == 1
    assert worker.items[0].size >= 320
    np.testing.assert_allclose(worker.items[0][:320], after_press)


def test_manual_hold_outranks_rejects_then_returns_to_auto_control():
    worker = ImmediateWorker()
    manual = [False]
    gate = make_gate(
        worker,
        manual_active=lambda: manual[0],
        stage1_inactivity_s=1.0,
    )
    block = np.full(320, 0.2, dtype=np.float32)

    gate.process(block, [activate_event(320)])
    manual[0] = True
    gate.process(block, [reject_event(640)])
    gate.process(block, [reject_event(960)])
    assert gate.active
    assert gate.consecutive_rejects == 0

    manual[0] = False
    gate.process(block, [reject_event(1280)])
    gate.process(block, [reject_event(1600)])
    assert not gate.active
    assert len(worker.items) == 1


def test_direct_controller_bypasses_detector_and_rolls_fixed_sessions():
    worker = ImmediateWorker()
    controller = DirectASRSessionController(worker, session_duration_s=0.03)
    block = np.full(320, 0.2, dtype=np.float32)  # 20 ms

    # Events are deliberately ignored: direct mode has no detector dependency.
    controller.process(block, [reject_event(320)])
    controller.process(block, [activate_event(640)])

    assert len(worker.items) == 1
    assert worker.items[0].size == 640
    controller.close()
    assert worker.closed


def test_direct_abort_discards_partial_session():
    worker = ImmediateWorker()
    controller = DirectASRSessionController(worker, session_duration_s=1.0)
    controller.process(np.full(320, 0.2, dtype=np.float32))

    controller.abort()
    controller.close()

    assert worker.items == []
    assert worker.closed
