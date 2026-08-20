# Third-party notices

Proximic Voice connects to third-party projects without committing their downloaded model weights.

## llama.cpp

- Source: https://github.com/ggml-org/llama.cpp
- Installed location: `.runtime/local-llm/runtimes/` (downloaded during Windows setup)
- License: MIT
- The pinned release URL and SHA-256 are recorded in `local_llm_catalog.json`.

## Qwen3-4B-Instruct-2507-GGUF

- Base model: https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507
- GGUF quantization: https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF
- Installed location: `.runtime/local-llm/models/` (downloaded during Windows setup)
- Model license: Apache License 2.0
- Model weights are not committed to Git; the pinned download URL and SHA-256 are recorded in
  `local_llm_catalog.json`.

## streaming-sensevoice

- Source: https://github.com/pengzhendong/streaming-sensevoice
- Location: `third_party/streaming-sensevoice` (vendored source snapshot)
- License: Apache License 2.0 (see the submodule's `LICENSE` file)

## Fun-ASR

- Source: https://github.com/QwenAudio/Fun-ASR
- Location: `third_party/Fun-ASR` (vendored source snapshot, model weights excluded)
- Source-code license: Apache License 2.0 (see the submodule's `LICENSE` file)
- Model weights: distributed separately; review the license shown on the selected ModelScope or
  Hugging Face model card before redistribution.

## Ringo Python SDK snapshot

- Location: `src/ring_python_sdk`
- Origin: supplied with the Ringo device integration used by this project.
- License: no standalone license file was found in the supplied snapshot. Redistribution permission
  must be confirmed with the SDK/device provider before this repository is made public.

Third-party names and trademarks belong to their respective owners.
