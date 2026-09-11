import json

from ring_whisper_lab.dataset import PairedAudioDataset, export_dataset
from ring_whisper_lab.demo import make_demo
from ring_whisper_lab.storage import read_json, write_json


def test_export_excludes_demo_and_requires_review_then_loads_pairs(tmp_path):
    take=make_demo(tmp_path)
    assert read_json(export_dataset(tmp_path)/"export.json")["segments"]==0
    r=read_json(take/"record.json")
    # Synthetic test fixture simulates an accepted real take only inside tmp_path.
    r["demo"]=False
    r["metadata"].update(speaker_id="p01",speech_style="whisper")
    write_json(take/"record.json",r)
    assert read_json(export_dataset(tmp_path)/"export.json")["segments"]==0
    r["review"]={"decision":"accepted","transcript":"示例","usable_start_s":1,"usable_end_s":14}
    write_json(take/"record.json",r)
    export=export_dataset(tmp_path)
    summary=read_json(export/"export.json")
    assert summary["segments"]==4
    split=summary["speaker_splits"]["p01"]
    dataset=PairedAudioDataset(export/f"{split}.jsonl",tmp_path)
    item=dataset[0]
    assert item["input"].shape==item["reference"].shape==(64000,)
    assert item["metadata"]["start_sample"]==16000
    assert item["metadata"]["transcript_scope"]=="entire_take_not_segment"
    r["metadata"]["configuration"]="both_bare"
    write_json(take/"record.json",r)
    assert read_json(export_dataset(tmp_path)/"export.json")["segments"]==0
