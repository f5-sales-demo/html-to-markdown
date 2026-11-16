# Private evidence storage

The queue retains complete requests, original captures, native media, OCR and
responses. Large image requests can consume substantial disk space when copied
into attempt, cache and article review records. Storage handling preserves those
logical bytes and all existing request, policy and source bindings.

Private writes use an atomic verified checkpoint with restrictive permissions and
file/directory synchronization. Supported Mac installations use the installed
`afsctool` to compress large staged files transparently. The writer checks logical
hashes, owners, modes and timestamps before publishing the file. Compression
tooling is optional; unsupported installations retain the complete ordinary JSON.
Operational storage receipts and interrupted evidence stay in the private
`~/.local/share/html-to-markdown/storage-events` directory, outside captured input
trees and consumer artifacts.

Concurrent inference jobs reserve space on their journal filesystem before new
transport calls. Insufficient storage produces an explicit resumable pending
record without executing inference. Native scratch files use the reserved journal
filesystem even when the system temporary directory is on another device. The
existing four-job and single-worker locks still apply. Interrupted scratch
directories preserve the complete native inputs and any already rendered output
when checkpoint creation itself fails. A complete valid cache can be reused even while new work is delayed.

The original request is persisted separately before inference. A completed
transport response and raw diagnostics are checkpointed before larger attempt and
cache copies. Resume reconstructs evidence only against the exact original bytes
and request hash, then repeats ordinary cache validation. Semantically rejected,
incomplete, corrupted or wrong-request responses cannot become approvals through
recovery. Attempt numbers include original completion and start records, so a
failed large copy or interrupted transport cannot overwrite a previous attempt.
Storage failures preserve private interrupted bytes and prior published
checkpoints; they never authorize another editorial proposal or a publication.

If a directory sync fails after rename, the failure explicitly reports committed
bytes with unverified durability. A retry verifies the committed checkpoint.

Source, privacy, retirement, preservation and packaging checks remain separate.
Raw storage and transformation evidence are private. Snapshot archives and Pages
contain reviewed content and safe provenance.
