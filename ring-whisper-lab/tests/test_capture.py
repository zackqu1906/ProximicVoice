import asyncio
import json
import struct

import numpy as np
import pytest

from ring_whisper_lab.capture import DualCapture, FrameArchive
from ring_whisper_lab.storage import read_json, read_wav


def packet(seq,value=1000,frag=0,count=1,payload=None):
    pcm=np.full(1600,value,dtype="<i2").tobytes() if payload is None else payload
    return struct.pack("<BBHHHI",0x20,2,seq,frag,count,seq*100)+pcm


def test_archive_reorders_frames_inserts_explicit_gap_and_keeps_raw(tmp_path):
    a=FrameArchive(tmp_path,"input","pcm")
    a.notify(packet(12,1200)); a.notify(packet(10,1000)); a.notify(packet(13,1300))
    a.notify(packet(13,1300))
    quality=a.finish()
    x=read_wav(tmp_path/"input.wav")
    assert len(x)==6400
    np.testing.assert_allclose(x[:1600],1000/32768)
    np.testing.assert_equal(x[1600:3200],0)
    np.testing.assert_allclose(x[3200:4800],1200/32768)
    assert quality["gaps"][0]["missing_frames"]==1
    assert len(read_wav(tmp_path/"input.capture.wav"))==4800
    rows=[json.loads(s) for s in (tmp_path/"input.frames.jsonl").read_text().splitlines()]
    assert rows[0]["frame_seq"]==12
    assert rows[0]["device_uptime_ms"]==1200
    assert (tmp_path/"input.notifications.bin").stat().st_size>0


def test_fragments_and_sequence_wrap(tmp_path):
    a=FrameArchive(tmp_path,"input","pcm")
    pcm=np.full(1600,2222,dtype="<i2").tobytes()
    a.notify(packet(65535,frag=1,count=2,payload=pcm[1600:]))
    a.notify(packet(65535,frag=0,count=2,payload=pcm[:1600]))
    a.notify(packet(0,3000))
    quality=a.finish()
    assert not quality["gaps"]
    x=read_wav(tmp_path/"input.wav")
    assert len(x)==3200
    np.testing.assert_allclose(x[:1600],2222/32768)


def test_incomplete_tail_is_reported_not_concealed(tmp_path):
    a=FrameArchive(tmp_path,"input","pcm")
    a.notify(packet(0))
    a.notify(packet(1,frag=0,count=2,payload=b"\x00"*100))
    q=a.finish()
    assert q["incomplete_frames"]==1
    assert len(read_wav(tmp_path/"input.wav"))==1600


def test_two_clients_capture_independently_and_stop_saves(monkeypatch,tmp_path):
    import bleak
    import ring_python_sdk.ble.control as control
    events=[]
    class Device:
        def __init__(self,address): self.address=address; self.name=address
    class Client:
        def __init__(self,device,**kwargs): self.device=device; self.is_connected=False; self.callback=None
        async def connect(self): self.is_connected=True
        async def start_notify(self,tx,callback): self.callback=callback
        async def disconnect(self): self.is_connected=False
    async def mic(client,rx,on,**kwargs):
        if on:
            for n in range(5):
                client.callback(0,packet(n,1000 if client.device.address=="a" else 2000))
    monkeypatch.setattr(bleak,"BleakClient",Client)
    monkeypatch.setattr(control,"ensure_nus_characteristics",lambda c:("tx","rx"))
    monkeypatch.setattr(control,"send_mic_control",mic)
    async def scenario():
        service=DualCapture(lambda k,v:events.append((k,v)))
        service.discovered={"a":Device("a"),"b":Device("b")}
        with pytest.raises(ValueError): await service.connect("a","a")
        await service.connect("a","b")
        await service.start(str(tmp_path),{"speaker_id":"p01","session_group":"s01"},"pcm")
        path=service.take
        assert set(service.clients)=={"input","reference"}
        await service.stop()
        assert read_json(path/"record.json")["status"]=="captured"
        np.testing.assert_allclose(read_wav(path/"raw/input.wav"),1000/32768)
        np.testing.assert_allclose(read_wav(path/"raw/reference.wav"),2000/32768)
        await service.disconnect()
    asyncio.run(scenario())


def test_partial_connect_failure_cleans_both_clients(monkeypatch):
    import bleak
    import ring_python_sdk.ble.control as control
    clients=[]
    class Device:
        def __init__(self,address): self.address=address; self.name=address
    class Client:
        def __init__(self,device,**kwargs): self.device=device; self.is_connected=False; clients.append(self)
        async def connect(self):
            if self.device.address=="bad": raise OSError("unavailable")
            self.is_connected=True
        async def start_notify(self,*args): pass
        async def disconnect(self): self.is_connected=False
    monkeypatch.setattr(bleak,"BleakClient",Client)
    monkeypatch.setattr(control,"ensure_nus_characteristics",lambda c:("tx","rx"))
    async def scenario():
        service=DualCapture(lambda *args:None)
        service.discovered={"good":Device("good"),"bad":Device("bad")}
        with pytest.raises(RuntimeError): await service.connect("good","bad")
        assert not service.clients
        assert all(not c.is_connected for c in clients)
    asyncio.run(scenario())
