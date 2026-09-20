"""Exercise the installed IME only in our disposable TransportProbe editor."""
import json
from pathlib import Path
import queue
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from proximic_ring.ime_bridge import IMEBridge

events = queue.Queue()
bridge = IMEBridge(events.put)
target = "com.proximic.IMETransportProbe"
log = ROOT / ".build/input-method/transport-host.jsonl"
client_id = None
utterance = uuid.uuid4().hex
sequence = 0
stage = "waiting"
deadline = time.monotonic() + 300
latest = {}
passed = False

def send(kind, **extra):
    global sequence
    # Every write is bound to the observed probe client and transaction; no
    # reset-all, source selection, synthetic keys, or other-app interaction.
    assert latest.get("application") == target and latest.get("client_id") == client_id
    sequence += 1
    assert bridge.send(dict(type=kind, client_id=client_id, utterance_id=utterance, seq=sequence, **extra))

try:
    bridge.start()
    print("等待独立诊断窗口手动选中 ProxiMic Voice；不会向其他应用写入。", flush=True)
    with log.open("w") as output:
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                continue
            # Do not persist unrelated applications' context.
            if event.get("type") == "state":
                latest = event
                if event.get("application") != target:
                    if stage != "waiting":
                        raise RuntimeError("诊断窗口失去焦点，已停止")
                    continue
            elif event.get("client_id") != client_id or client_id is None:
                continue
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            if event.get("type") == "state" and event.get("phase") == "error":
                print("FAILED: " + event.get("error", ""), flush=True)
                break
            if stage == "waiting" and event.get("type") == "state" and event.get("ready"):
                client_id = event["client_id"]
                stage = "begin"
                send("begin")
            elif stage == "begin" and event.get("phase") == "listening":
                stage = "partial"
                send("update", text="把三点改成四点", final=False)
            elif stage == "partial" and event.get("has_composition"):
                stage = "convert"
                send("convert")
            elif stage == "convert" and event.get("type") == "finish_audio":
                stage = "final"
                send("update", text="把三点改成四点", final=True)
            elif stage == "final" and event.get("type") == "edit_requested":
                assert event["original"] == "今天三点开会", "Fixture changed; abort"
                stage = "edit"
                send("edit_result", revision=event["revision"], text="今天四点开会")
            elif stage == "edit" and event.get("phase") == "edited":
                stage = "restore"
                send("cancel")
            elif stage == "restore" and event.get("phase") == "dictated":
                stage = "undo"
                send("cancel")
            elif stage == "undo" and event.get("phase") == "undone":
                passed = True
                print("PASS: real IMK edit, restore dictation, undo", flush=True)
                break
        else:
            print("等待超时", flush=True)
finally:
    bridge.close()
raise SystemExit(0 if passed else 1)
