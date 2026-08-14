# Third-party source snapshots

This directory vendors the two ASR source trees that were used during local integration testing:

- `streaming-sensevoice`: https://github.com/pengzhendong/streaming-sensevoice
- `Fun-ASR`: https://github.com/QwenAudio/Fun-ASR

Their original `LICENSE` and README files are preserved. Downloaded model checkpoints, Python
caches and local runtime data are deliberately excluded from this repository.

When refreshing a snapshot, compare the new upstream source against the adapter contracts in
`src/proximic_ring/asr/backends/`, rerun the automated tests, and perform one real streaming
session before committing the update.
