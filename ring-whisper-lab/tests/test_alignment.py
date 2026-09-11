from pathlib import Path

import numpy as np
import pytest
from scipy import signal

from ring_whisper_lab.alignment import align_take, estimate_alignment
from ring_whisper_lab.demo import make_demo
from ring_whisper_lab.storage import read_json, read_wav, write_json


@pytest.mark.parametrize("delay,ppm",[(700,180),(-900,-220),(0,0)])
def test_recovers_delay_and_drift_with_wind_and_channel_gain(delay,ppm):
    rng=np.random.default_rng(11)
    n=12*16000
    clean=signal.sosfilt(signal.butter(3,[500,5000],btype="bandpass",fs=16000,output="sos"),rng.normal(size=n))*.08
    y=.67*np.interp((np.arange(n)-delay)/(1+ppm/1e6),np.arange(n),clean,left=0,right=0)
    t=np.arange(n)/16000
    wind=signal.sosfilt(signal.butter(2,120,fs=16000,output="sos"),rng.normal(size=n))*.4*np.exp(-((t-5)/.4)**2)
    result=estimate_alignment(clean+wind,y)
    assert abs(result["offset_samples"]-delay)<3
    assert abs(result["drift_ppm"]-ppm)<15
    assert result["automatic_quality_pass"],result


def test_silence_and_unrelated_audio_never_auto_pass():
    n=6*16000
    rng=np.random.default_rng(12)
    assert not estimate_alignment(np.zeros(n),np.zeros(n))["automatic_quality_pass"]
    assert not estimate_alignment(rng.normal(size=n),rng.normal(size=n))["automatic_quality_pass"]


def test_alignment_preserves_raw_and_creates_equal_length_revision(tmp_path):
    take=make_demo(tmp_path)
    before=(take/"raw/input.wav").read_bytes()
    old=read_json(take/"record.json")["alignment"]["revision"]
    result=align_take(take)
    assert (take/"raw/input.wav").read_bytes()==before
    assert result["revision"] != old
    assert (take/old/"input.wav").exists()
    x=read_wav(take/result["revision"]/"input.wav")
    y=read_wav(take/result["revision"]/"reference.wav")
    assert len(x)==len(y)==result["sample_count"]
    assert result["automatic_quality_pass"]


def test_manual_transform_and_damaged_capture_require_review(tmp_path):
    take=make_demo(tmp_path)
    r=read_json(take/"record.json")
    r["quality"]["input"]["gaps"]=[{"start_sample":0,"end_sample":1600}]
    write_json(take/"record.json",r)
    assert not align_take(take)["automatic_quality_pass"]
    result=align_take(take,manual_offset_ms=38.3125,manual_drift_ppm=180)
    assert result["method"]=="manual_affine"
    assert not result["automatic_quality_pass"]


def test_short_input_rejected():
    with pytest.raises(ValueError): estimate_alignment(np.zeros(10),np.zeros(10))
