from proximic_ring.audio.microphone import list_input_devices

for index, name, sr, channels in list_input_devices():
    print(f"{index:3d}  inputs={channels:<2d} default_sr={sr:8.1f}  {name}")
