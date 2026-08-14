# Copyright (c) 2024, Zhendong Peng (pzd17@tsinghua.org.cn)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import soundfile as sf

from streaming_sensevoice import StreamingSenseVoice


def main():
    contexts = ["停止"]
    model = StreamingSenseVoice(contexts=contexts)

    samples, sr = sf.read("data/test_16k.wav")
    samples = (samples * 32768).tolist() * 3

    step = int(0.1 * sr)
    for i in range(0, len(samples), step):
        is_last = i + step >= len(samples)
        for res in model.streaming_inference(samples[i : i + step], is_last):
            print(res["timestamps"])
            print(res["text"])


if __name__ == "__main__":
    main()
