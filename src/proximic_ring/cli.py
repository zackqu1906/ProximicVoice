from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audio import MicrophoneSource, RingAudioSource, WavSource
from .collect import CollectionConfig, collect_ring_dataset
from .config import DetectorConfig
from .detector import ProxiMicDetector
from .model import ProxiMicModel
from .pipeline import LegacyInferencePipeline
from .runner import run_source
from .train import train_proximity_model
from .asr.console import StreamingASRConsole


def _device_value(value: str | None):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _custom_model_threshold(model_path: Path | None) -> float | None:
    if model_path is None:
        return None
    sidecar = model_path.with_name(model_path.name + ".json")
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        value = payload.get("recommended_stage2_threshold")
        if value is None:
            return None
        return float(value)
    except Exception as exc:
        print(f"WARNING: could not read model sidecar {sidecar}: {exc}")
        return None


def _add_detector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", type=Path, default=None, help="Optional replacement PyTorch state_dict")
    parser.add_argument("--stage1-threshold", type=float, default=0.30)
    parser.add_argument(
        "--stage2-threshold",
        type=float,
        default=None,
        help=(
            "Stage-2 score threshold. Default: 1.0 for the bundled legacy model; "
            "for a trained custom model, auto-load <model>.json when present."
        ),
    )
    parser.add_argument("--stage2-delay", type=float, default=0.50, help="Seconds after Stage-1 trigger")
    parser.add_argument("--show-stage1", action="store_true")


def _add_ring_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="Ringo", help="BLE device-name keyword used by SDK scan")
    parser.add_argument(
        "--selector",
        default=None,
        help="Optional exact SDK selector (scan index, name substring, or BLE address)",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="BLE scan/connect timeout in seconds")
    parser.add_argument(
        "--encoding",
        choices=["pcm", "adpcm", "opus"],
        default="pcm",
        help="Ring MIC transport codec. pcm is simplest and recommended for dataset collection.",
    )


def _build_detector(args) -> ProxiMicDetector:
    threshold = args.stage2_threshold
    if threshold is None:
        threshold = _custom_model_threshold(args.model)
        if threshold is not None:
            print(f"Loaded Stage-2 threshold {threshold:+.6f} from {args.model.name}.json")
        else:
            threshold = 1.0

    cfg = DetectorConfig(
        stage1_threshold=args.stage1_threshold,
        stage2_threshold=threshold,
        stage2_delay_s=args.stage2_delay,
    ).validate()
    model = ProxiMicModel(args.model) if args.model else ProxiMicModel()
    return ProxiMicDetector(cfg, LegacyInferencePipeline(model=model))


def _add_asr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--asr",
        "--asr-backend",
        dest="asr",
        action="append",
        default=None,
        metavar="BACKEND",
        help=(
            "ASR backend. Batch backends run on session END; streaming backends can emit partial "
            "text while the ProxiMic session is active. Repeat to run multiple independent backends. "
            "Use 'off' or omit this option to disable ASR."
        ),
    )
    parser.add_argument(
        "--asr-model",
        action="append",
        default=None,
        metavar="[BACKEND=]MODEL",
        help=(
            "Optional model override. With one backend use --asr-model MODEL. With multiple "
            "backends use --asr-model sensevoice=... --asr-model whisper=... ."
        ),
    )
    parser.add_argument("--asr-device", default="cuda:0", help="Local ASR device, e.g. cuda:0 or cpu")
    parser.add_argument("--asr-language", default="auto", choices=["auto", "zh", "en", "yue", "ja", "ko"])
    parser.add_argument(
        "--asr-option",
        action="append",
        default=None,
        metavar="[BACKEND.]KEY=VALUE",
        help=(
            "Backend-specific option. Repeat as needed. When multiple backends are selected, "
            "prefix the backend name, e.g. --asr-option http.url=https://... ."
        ),
    )
    parser.add_argument(
        "--sensevoice-repo",
        type=Path,
        default=None,
        help="Compatibility shortcut for --asr-option sensevoice.repo=PATH.",
    )
    parser.add_argument(
        "--streaming-sensevoice-repo",
        type=Path,
        default=None,
        help=(
            "Path to a pengzhendong/streaming-sensevoice checkout. Compatibility shortcut "
            "for --asr-option streaming_sensevoice.repo=PATH."
        ),
    )
    parser.add_argument(
        "--funasr-nano-repo",
        type=Path,
        default=None,
        help=(
            "Path to a Fun-ASR source checkout containing model.py. Compatibility shortcut "
            "for --asr-option funasr_nano.repo=PATH."
        ),
    )
    parser.add_argument("--asr-pre-roll", type=float, default=1.0, help="Seconds retained before first ACTIVATE")
    parser.add_argument(
        "--asr-end-rejects",
        type=int,
        default=2,
        help="Consecutive Stage2 rejects required to end an active near-speech session.",
    )
    parser.add_argument(
        "--asr-stage1-inactivity",
        type=float,
        default=1.25,
        help=(
            "Fallback seconds with no Stage1 trigger before ending. Needed because true silence "
            "produces no Stage2 reject at all."
        ),
    )
    parser.add_argument("--asr-min-duration", type=float, default=0.40)
    parser.add_argument("--asr-max-duration", type=float, default=15.0)
    parser.add_argument(
        "--disable-proximic-detector",
        "--disable-detector",
        action="store_true",
        help=(
            "Baseline mode: bypass ProxiMic entirely and continuously send Ring/WAV/mic audio "
            "to --asr. Audio rolls into fixed ASR sessions; requires at least one ASR backend."
        ),
    )
    parser.add_argument(
        "--direct-asr-session-duration",
        type=float,
        default=5.0,
        help="Seconds per direct-ASR baseline session when --disable-proximic-detector (default: 5).",
    )
    parser.add_argument(
        "--asr-partial-min-interval",
        type=float,
        default=1.0,
        help=(
            "When comparing multiple streaming ASRs, minimum seconds between local "
            "streaming_sensevoice partial lines (default: 1.0; use 0 for every update)."
        ),
    )
    parser.add_argument(
        "--desktop-output",
        action="store_true",
        help=(
            "Windows: show streaming partial text in a non-activating overlay and type only "
            "the final result into the currently focused input control. The clipboard is untouched."
        ),
    )
    parser.add_argument(
        "--desktop-output-backend",
        default=None,
        metavar="BACKEND",
        help=(
            "Streaming backend used for desktop output. Required when more than one ASR backend "
            "is selected with --desktop-output."
        ),
    )
    parser.add_argument(
        "--push-to-talk",
        action="store_true",
        help=(
            "Windows: hold right Alt to force the existing ProxiMic ASR session active. "
            "Release returns endpoint control to the automatic detector."
        ),
    )


def _selected_asr_backends(args) -> list[str]:
    selected = [str(x).strip().lower().replace("-", "_") for x in (getattr(args, "asr", None) or [])]
    selected = [x for x in selected if x]
    if not selected or selected == ["off"]:
        return []
    if "off" in selected:
        raise ValueError("--asr off cannot be combined with another ASR backend")
    # Preserve CLI order but avoid accidental duplicate model loads.
    return list(dict.fromkeys(selected))


def _format_asr_result(result) -> None:
    if result.error:
        print(
            f"ASR[{result.backend}/{result.model}] ERROR "
            f"latency={result.latency_s * 1000:.0f}ms: {result.error}"
        )
        return
    print(
        f"ASR[{result.backend}/{result.model}] "
        f"latency={result.latency_s * 1000:.0f}ms rtf={result.rtf:.3f}: {result.text}"
    )


def _build_session_controller(
    args,
    detector,
    *,
    streaming_observer=None,
    desktop_overlay=None,
    on_state=print,
    show_streaming_console: bool = True,
    push_to_talk_observer=None,
    desktop_should_inject=None,
    backend_cache=None,
    raw_audio_observer=None,
):
    selected = _selected_asr_backends(args)
    if not selected:
        return None

    from .asr import (
        ASRBackendSettings,
        ASRFanout,
        ASRWorker,
        CompletedUtteranceSessionSink,
        ProximitySessionController,
        RawAudioObserverSessionSink,
        DirectASRSessionController,
        SessionFanout,
        StreamingASRWorker,
        asr_backend_kind,
        create_asr_backend,
        create_streaming_asr_backend,
    )
    from .asr.factory import parse_backend_options, parse_model_overrides

    option_map = parse_backend_options(getattr(args, "asr_option", None), selected)
    model_map = parse_model_overrides(getattr(args, "asr_model", None), selected)
    streaming_console = StreamingASRConsole(
        selected=selected,
        local_partial_interval_s=args.asr_partial_min_interval,
    )
    desktop_output = None
    if args.desktop_output:
        import os

        if os.name != "nt":
            raise ValueError("--desktop-output is currently supported only on Windows")
        output_backend = (
            str(args.desktop_output_backend).strip().lower().replace("-", "_")
            if args.desktop_output_backend
            else None
        )
        if output_backend is None:
            if len(selected) != 1:
                raise ValueError(
                    "--desktop-output-backend is required when multiple ASR backends are selected"
                )
            output_backend = selected[0]
        if output_backend not in selected:
            raise ValueError("--desktop-output-backend must name one of the selected --asr backends")
        if asr_backend_kind(output_backend) != "streaming":
            raise ValueError("--desktop-output requires a streaming ASR backend")

        from .desktop_output import DesktopTranscriptOutput

        desktop_output = DesktopTranscriptOutput(
            backend=output_backend,
            overlay=desktop_overlay,
            on_error=on_state,
            should_inject=desktop_should_inject,
        )

    def publish_streaming_update(update) -> None:
        if show_streaming_console:
            streaming_console(update)
        if streaming_observer is not None:
            streaming_observer(update)
        if desktop_output is not None:
            desktop_output(update)

    # Compatibility shortcuts only populate backend-local options.  ProxiMic
    # and the generic session controller never import either SenseVoice adapter.
    if args.sensevoice_repo is not None:
        if "sensevoice" not in selected:
            raise ValueError("--sensevoice-repo requires --asr sensevoice")
        option_map["sensevoice"]["repo"] = str(args.sensevoice_repo)

    if args.streaming_sensevoice_repo is not None:
        if "streaming_sensevoice" not in selected:
            raise ValueError(
                "--streaming-sensevoice-repo requires --asr streaming_sensevoice"
            )
        option_map["streaming_sensevoice"]["repo"] = str(args.streaming_sensevoice_repo)

    if args.funasr_nano_repo is not None:
        if "funasr_nano" not in selected:
            raise ValueError("--funasr-nano-repo requires --asr funasr_nano")
        option_map["funasr_nano"]["repo"] = str(args.funasr_nano_repo)

    batch_workers = []
    session_sinks = []
    if raw_audio_observer is not None:
        # Dataset audio and ASR both receive the Controller's final cropped
        # 16 kHz waveform without a separate amplitude transform.
        session_sinks.append(RawAudioObserverSessionSink(raw_audio_observer))

    for name in selected:
        settings = ASRBackendSettings(
            model=model_map.get(name),
            device=args.asr_device,
            language=args.asr_language,
            options=option_map.get(name, {}),
            status_callback=on_state,
        )
        kind = asr_backend_kind(name)
        backend = (
            backend_cache.get(name, kind, settings)
            if backend_cache is not None
            else None
        )

        if backend is None:
            on_state(f"正在加载语音模型 {name}（{kind}）…")
            if kind == "batch":
                backend = create_asr_backend(name, settings)
            elif kind == "streaming":
                backend = create_streaming_asr_backend(name, settings)
            else:  # pragma: no cover - factory invariant
                raise AssertionError(kind)
            if backend_cache is not None:
                backend_cache.put(name, kind, settings, backend)
        else:
            on_state(f"正在复用已加载语音模型 {name}…")

        if kind == "batch":
            batch_workers.append(ASRWorker(backend, on_result=_format_asr_result))
        elif kind == "streaming":
            session_sinks.append(
                StreamingASRWorker(
                    backend,
                    on_update=publish_streaming_update,
                    on_state=on_state,
                )
            )
        else:  # pragma: no cover - factory invariant
            raise AssertionError(kind)

        on_state(f"语音模型已就绪：{backend.backend_name}/{backend.model_name}")

    if batch_workers:
        batch_sink = batch_workers[0] if len(batch_workers) == 1 else ASRFanout(batch_workers)
        session_sinks.append(CompletedUtteranceSessionSink(batch_sink))

    push_to_talk = None
    if args.push_to_talk:
        import os

        if os.name != "nt":
            raise ValueError("--push-to-talk is currently supported only on Windows")
        if args.disable_proximic_detector:
            raise ValueError("--push-to-talk cannot be combined with --disable-proximic-detector")
        from .push_to_talk import WindowsPushToTalkHotkey

        push_to_talk = WindowsPushToTalkHotkey(
            on_error=on_state,
            on_change=push_to_talk_observer,
        )
        session_sinks.append(push_to_talk)

    if desktop_output is not None:
        from .desktop_output import DesktopOutputLifecycleSink

        # Fanout closes sinks in order.  Appending this last keeps the overlay
        # alive until every ASR worker has emitted its queued final result.
        session_sinks.append(DesktopOutputLifecycleSink(desktop_output))

    sink = session_sinks[0] if len(session_sinks) == 1 else SessionFanout(session_sinks)
    if args.disable_proximic_detector:
        return DirectASRSessionController(
            sink,
            session_duration_s=args.direct_asr_session_duration,
            on_state=on_state,
        )

    if detector is None:  # pragma: no cover - CLI invariant
        raise AssertionError("ProxiMic detector is required unless disabled")
    return ProximitySessionController(
        sink,
        pre_roll_s=args.asr_pre_roll,
        end_rejects=args.asr_end_rejects,
        stage1_inactivity_s=args.asr_stage1_inactivity,
        stage2_delay_s=detector.config.stage2_delay_s,
        min_utterance_s=args.asr_min_duration,
        max_utterance_s=args.asr_max_duration,
        on_state=on_state,
        manual_active=push_to_talk.is_active if push_to_talk is not None else None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proximic-ring",
        description="ProxiMic inference, Ringo dataset collection, and model training.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("asr-backends", help="List built-in ASR backend adapter modules")

    p = sub.add_parser("ring", help="Stream the Ringo microphone through ring-python-sdk")
    _add_ring_connection_args(p)
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="SDK capture root. The SDK saves a WAV here while inference uses the live callback.",
    )
    _add_detector_args(p)
    _add_asr_args(p)

    p = sub.add_parser(
        "record",
        help="Record raw Ringo microphone audio without ProxiMic, ASR, or LLM",
    )
    _add_ring_connection_args(p)
    p.add_argument(
        "--duration",
        type=_positive_float,
        default=10.0,
        help="Recording duration in seconds (default: 10)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Capture root; WAV is saved under data/session/<timestamp>/",
    )

    p = sub.add_parser("wav", help="Replay a 16 kHz PCM16 WAV file")
    p.add_argument("path", type=Path)
    _add_detector_args(p)
    _add_asr_args(p)

    p = sub.add_parser("mic", help="Use a normal OS-visible microphone as a local baseline")
    p.add_argument("--device", default=None, help="sounddevice index or device-name substring")
    _add_detector_args(p)
    _add_asr_args(p)

    p = sub.add_parser("collect", help="Collect near/far/artifact Ringo WAV clips for binary retraining")
    _add_ring_connection_args(p)
    p.add_argument("--dataset", type=Path, default=Path("datasets/ring_proximity"))
    p.add_argument("--label", choices=["near", "far", "artifact"], required=True)
    p.add_argument("--distance-cm", type=float, default=0.0, help="Required for near/far; optional for artifact")
    p.add_argument("--speaker", required=True, help="Subject/operator ID, e.g. qzy or p01; retained as metadata even for artifact")
    p.add_argument(
        "--style",
        default="normal",
        help="Subtype/style label, e.g. normal, whisper, airflow, hand_motion, fabric_rub",
    )
    p.add_argument("--angle-deg", type=float, default=0.0)
    p.add_argument("--count", type=int, default=6, help="Number of long takes for this condition")
    p.add_argument("--duration", type=float, default=8.0, help="Seconds per long labeled take")
    p.add_argument("--countdown", type=int, default=3, help="Countdown seconds before each take")
    p.add_argument("--gap", type=float, default=2.0, help="Minimum rest seconds after each take")
    p.add_argument(
        "--auto-next",
        action="store_true",
        help="Do not wait for Enter before each take; use fixed gap + countdown instead.",
    )
    p.add_argument("--near-max-cm", type=float, default=5.0)
    p.add_argument("--far-min-cm", type=float, default=20.0)
    p.add_argument("--allow-ambiguous-distance", action="store_true")
    p.add_argument("--phrases", "--prompts", dest="phrases", type=Path, default=None, help="Optional UTF-8 prompt file, one speech/action prompt per line")
    p.add_argument("--notes", default="")

    p = sub.add_parser("train", help="Train CnnNet8 for near speech vs realistic non-target audio")
    p.add_argument("--dataset", type=Path, default=Path("datasets/ring_proximity"))
    p.add_argument("--run-dir", type=Path, default=Path("runs/ringo_proximic_v1"))
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--init", choices=["pretrained", "scratch"], default="scratch")
    p.add_argument("--init-checkpoint", type=Path, default=None)
    p.add_argument("--device", default="auto", help="auto, cpu, cuda, cuda:0, ...")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--split-by",
        choices=["auto", "speaker", "file", "segment"],
        default="file",
        help=(
            "Split unit. file keeps each original WAV intact; segment logically cuts long WAVs "
            "into fixed-duration pseudo-takes before train/val/test; speaker is speaker-disjoint."
        ),
    )
    p.add_argument(
        "--split-segment-duration",
        type=float,
        default=8.0,
        help="Pseudo-take duration in seconds when --split-by segment (default: 8).",
    )
    p.add_argument("--val-fraction", type=float, default=0.15)
    p.add_argument("--test-fraction", type=float, default=0.15)
    p.add_argument("--patience", type=int, default=8)
    p.add_argument("--num-workers", type=int, default=0, help="Use 0 on Windows unless needed")
    p.add_argument(
        "--window-hop",
        type=float,
        default=0.50,
        help="Seconds between base 1-s windows extracted from each long take",
    )
    p.add_argument(
        "--edge-margin",
        type=float,
        default=0.50,
        help="Seconds ignored at the start/end of each long take",
    )
    p.add_argument(
        "--train-jitter",
        type=float,
        default=0.15,
        help="Random +/- seconds applied to training window starts each epoch",
    )
    p.add_argument(
        "--noise-dir",
        type=Path,
        default=None,
        help="Directory of background-noise WAVs. When set, noise is mixed on-the-fly without changing raw recordings.",
    )
    p.add_argument(
        "--noise-prob",
        type=float,
        default=1.0,
        help="Probability of adding background noise to each window when --noise-dir is set.",
    )
    p.add_argument("--noise-snr-min-db", type=float, default=12.0)
    p.add_argument("--noise-snr-max-db", type=float, default=25.0)
    p.add_argument(
        "--no-noise-eval",
        action="store_false",
        dest="noise_eval",
        help="Mix noise only into training windows; by default val/test also receive disjoint deterministic noise.",
    )
    p.set_defaults(noise_eval=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "asr-backends":
        from .asr import available_asr_backends

        print("Available ASR backends:")
        for name in available_asr_backends():
            print(f"  {name}")
        return 0

    if args.command == "collect":
        cfg = CollectionConfig(
            dataset_root=args.dataset,
            label=args.label,
            distance_cm=args.distance_cm,
            speaker_id=args.speaker,
            speech_style=args.style,
            angle_deg=args.angle_deg,
            count=args.count,
            duration_s=args.duration,
            countdown_s=args.countdown,
            gap_s=args.gap,
            manual_ready=not args.auto_next,
            near_max_cm=args.near_max_cm,
            far_min_cm=args.far_min_cm,
            allow_ambiguous_distance=args.allow_ambiguous_distance,
            notes=args.notes,
        )
        return collect_ring_dataset(
            cfg=cfg,
            name_keyword=args.name,
            selector=args.selector,
            timeout_s=args.timeout,
            encoding=args.encoding,
            phrases_path=args.phrases,
        )

    if args.command == "train":
        return train_proximity_model(
            dataset_root=args.dataset,
            run_dir=args.run_dir,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            weight_decay=args.weight_decay,
            init=args.init,
            init_checkpoint=args.init_checkpoint,
            device_name=args.device,
            seed=args.seed,
            split_by=args.split_by,
            split_segment_duration_s=args.split_segment_duration,
            val_fraction=args.val_fraction,
            test_fraction=args.test_fraction,
            patience=args.patience,
            num_workers=args.num_workers,
            window_hop_s=args.window_hop,
            edge_margin_s=args.edge_margin,
            train_jitter_s=args.train_jitter,
            noise_dir=args.noise_dir,
            noise_probability=args.noise_prob,
            noise_snr_min_db=args.noise_snr_min_db,
            noise_snr_max_db=args.noise_snr_max_db,
            noise_eval=args.noise_eval,
        )

    if args.command == "record":
        source = RingAudioSource(
            name_keyword=args.name,
            selector=args.selector,
            timeout_s=args.timeout,
            encoding=args.encoding,
            data_root=args.data_dir,
        )
        target_samples = int(round(args.duration * source.sample_rate))
        recorded_samples = 0
        try:
            with source:
                print(f"Recording raw Ring audio for {args.duration:g}s ...")
                while recorded_samples < target_samples:
                    block = source.read(
                        min(320, target_samples - recorded_samples)
                    )
                    if block is None:
                        break
                    recorded_samples += int(block.size)
        except KeyboardInterrupt:
            print("\nRecording stopped by user.")

        capture = (
            str(source.capture_path)
            if source.capture_path is not None
            else "(none)"
        )
        print(
            f"Recorded {recorded_samples / source.sample_rate:.3f}s; "
            f"WAV saved to: {capture}"
        )
        return 0

    selected_asr = _selected_asr_backends(args)
    if args.disable_proximic_detector and not selected_asr:
        parser.error("--disable-proximic-detector requires --asr BACKEND")
    if (args.desktop_output or args.push_to_talk) and not selected_asr:
        parser.error("--desktop-output/--push-to-talk requires --asr BACKEND")
    if args.push_to_talk and args.disable_proximic_detector:
        parser.error("--push-to-talk cannot be combined with --disable-proximic-detector")
    detector = None if args.disable_proximic_detector else _build_detector(args)

    if args.command == "ring":
        source = RingAudioSource(
            name_keyword=args.name,
            selector=args.selector,
            timeout_s=args.timeout,
            encoding=args.encoding,
            data_root=args.data_dir,
        )
    elif args.command == "wav":
        source = WavSource(args.path)
    elif args.command == "mic":
        source = MicrophoneSource(_device_value(args.device))
    else:
        raise AssertionError(args.command)

    session_controller = _build_session_controller(args, detector)

    try:
        run_source(
            source,
            detector,
            show_stage1=args.show_stage1,
            audio_observer=session_controller,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if session_controller is not None:
            session_controller.close()

    if detector is not None:
        s = detector.stats
        print(
            f"samples={s.input_samples} stage1={s.stage1_triggers} "
            f"stage2_runs={s.stage2_runs} activations={s.activations}"
        )

    if isinstance(source, RingAudioSource):
        capture = str(source.capture_path) if source.capture_path is not None else "(none)"
        print(
            f"ring_pcm_callbacks={source.pcm_callbacks} "
            f"ring_samples_received={source.samples_received} "
            f"sdk_capture_wav={capture}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
