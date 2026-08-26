from pathlib import Path

import numpy as np

from proximic_ring import cli


def test_record_command_parses_duration_and_ring_options():
    args = cli.build_parser().parse_args(
        [
            "record",
            "--duration",
            "20",
            "--selector",
            "RING-ID",
            "--encoding",
            "adpcm",
        ]
    )

    assert args.command == "record"
    assert args.duration == 20.0
    assert args.selector == "RING-ID"
    assert args.encoding == "adpcm"


def test_record_command_only_reads_ring_audio(monkeypatch, tmp_path, capsys):
    created = []

    class FakeRingAudioSource:
        sample_rate = 16_000

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.capture_path = tmp_path / "session" / "ring_audio.wav"
            self.closed = False
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self.closed = True

        def read(self, frames):
            return np.zeros(frames, dtype=np.float32)

    monkeypatch.setattr(cli, "RingAudioSource", FakeRingAudioSource)
    monkeypatch.setattr(
        cli,
        "_build_detector",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("record must not build ProxiMic")
        ),
    )

    result = cli.main(
        [
            "record",
            "--duration",
            "0.02",
            "--data-dir",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert len(created) == 1
    assert created[0].kwargs["data_root"] == Path(tmp_path)
    assert created[0].closed is True
    output = capsys.readouterr().out
    assert "Recorded 0.020s" in output
    assert "ring_audio.wav" in output
