"""Synthetic transport-free smoke test; never masquerades as real speech."""
from pathlib import Path

import numpy as np
from scipy import signal

from .alignment import align_take
from .storage import SAMPLE_RATE, audio_stats, new_take, read_json, utc_now, write_json, write_wav


def make_demo(root: Path) -> Path:
    rng = np.random.default_rng(704)
    sr = SAMPLE_RATE
    seconds = 16
    t = np.arange(seconds*sr)/sr
    noise = signal.sosfilt(signal.butter(3,[500,4500],fs=sr,btype="bandpass",output="sos"),rng.normal(size=len(t)))
    envelope = .04 + .08*np.sin(t*3.3)**2
    clean = noise*envelope
    offset, drift = 613., 180e-6
    ref_indices = (np.arange(len(t))-offset)/(1+drift)
    reference = .82*np.interp(ref_indices,np.arange(len(t)),clean,left=0,right=0)
    gust = signal.sosfilt(signal.butter(2,180,fs=sr,output="sos"),rng.normal(size=len(t)))
    gust *= .45*np.exp(-((t-5)/.3)**2) + .35*np.exp(-((t-11)/.5)**2)
    source = clean+gust
    path = new_take(root,{"speaker_id":"DEMO","session_group":"synthetic","configuration":"bare_protected",
                          "speech_style":"synthetic","prompt":"合成测试信号，不是真实耳语"},
                    {"input":{"id":"demo-a"},"reference":{"id":"demo-b"}},demo=True)
    quality = {}
    for role, x in (("input",source),("reference",reference)):
        write_wav(path/"raw"/f"{role}.wav",x)
        quality[role] = {**audio_stats(x),"gaps":[],"errors":[],"incomplete_frames":0}
    record=read_json(path/"record.json")
    record.update(status="captured",ended_at=utc_now(),quality=quality,
                  simulation_truth={"offset_samples":offset,"drift_ppm":drift*1e6})
    write_json(path/"record.json",record)
    align_take(path)
    return path


def make_sync_tone(path: Path):
    t=np.arange(int(.45*SAMPLE_RATE))/SAMPLE_RATE
    tone=.16*signal.chirp(t,f0=650,f1=4200,t1=t[-1])*np.hanning(len(t))
    write_wav(path,np.concatenate((np.zeros(1600),tone,np.zeros(1600))))
