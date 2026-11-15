# Bounded independent curation validation

Large articles use bounded fact responses while every request retains the complete source,
candidate stages, policy evidence and media. Assignments keep structural sections separate,
limit each response to 48 facts, and preserve distinct coincident block occurrences. A separate
final request reviews document-wide coherence, technical relationships, privacy and retirement.
The controller checks exact complete coverage and source/candidate digest witnesses before
accepting the combined result. Section approval cannot bypass the final review or upstream
source admission and downstream actual-planner/publication gates.

The controller preflights the complete final request before starting section inference. Its final
ledger references exact source and candidate block indices, and reconstructs the entire ordered
fact ledger against its original digest. Complete article text, candidate stages and media remain
in the request; indexing removes repeated addresses and digests without changing decisions.
Unknown representations, malformed indices, duplicate witnesses and changed digests fail closed.
The native input limit remains 1 MiB.

Completed version-1 section requests can be reused only under the exact immutable section
controller profile with every other execution file, source request, candidate and assignment
unchanged. Every captured section body must still match its reconstructed request. The separately
required final request pins the current validation and ledger controllers; historical sections alone
cannot approve a document.

Incomplete independent validation preserves the completed editor proposal and its correction
ledger. Interrupted transport resumes completed sections. Every failed response remains private;
rejected cache entries cannot confer approval. Both ordinary single-request and sectioned results
replay from their exact captured requests without inference.

A completed historical proposal can receive validation-only selection:

```bash
html-to-markdown validate-existing-curation \
  --output private-curation-review \
  --original-binding ORIGINAL_CONTEXT_SHA256 \
  --attempt 2 \
  --journal private-model-journal
```

Add `--replay` to use complete recorded evidence on Ubuntu with zero inference. The original
input files, policy digest, editor request, proposal and candidate must verify. Selection recompiles
the same transformations through the current compiler under a new binding envelope, and rejects
any difference in actual text, stages or media transformations. It never rewrites or rebinds the
original model response. The original state, failed attempts and exhausted correction ledger stay
unchanged; the separately selected state retains their attempt and feedback counts. The independently
validated selection replays through the ordinary curation planner interface.

All inference uses Mac-installed Codex and the configured LiteLLM Responses provider with
GPT-6.1-Sol High. Raw requests, media, responses and transformation evidence remain private.
The CLI returns hashes and counts; it does not print article text or claim publication approval.
