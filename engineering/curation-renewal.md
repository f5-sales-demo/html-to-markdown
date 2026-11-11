# Independently validated curation catalog renewal

Parent delivery is #155; implementation and public-queue resolution are tracked in #167.

The current catalogs bind older source and media bytes. Admission must preserve the privacy
source and queue missing or stale retirement guards instead of waiving them. Renewal proposals
use the installed Mac Codex transport and the existing two-model, high-reasoning, four-job
limit. Every article, original source classification and media placement remains untrusted data.

The controller supplies structural addresses and digest guards and retrieves exact source
spans. Topic decisions follow legacy, SMSv2, AppStack, vesctl and Terraform order. Proposals
remain private until independent source, privacy, preservation and whole-corpus planner
validation passes. A policy change invalidates old requests. Preserve every correction, reject
incomplete/refused responses and use at most two corrected proposals.

Public changes contain sanitized catalog entries, source/output/media digests and policy pins.
Private responses, OCR, captured HTML and transformation evidence never enter repository files
or consumer archives. Coherent batches use linked PRs and normal CI/squash governance.

## Work and acceptance

- [x] Prepare complete digest-bound review inputs from queued admission records.
- [x] Compile exact structural topic and media proposals without model-generated hashes.
- [x] Independently validate complete preservation and resulting retirement policy.
- [x] Preserve attempts and replay exact requests offline.
- [x] Qualify the actual planner against the verified baseline; queue unknown dependency changes.
- [x] Stage sanitized catalog/pin changes and prepare linked policy PRs on Ubuntu.
- [ ] Resume affected admission using merged pins and reconcile the public queue.
- [ ] Verify deterministic repeated curation, packaging and immutable publication.

Passing source articles require no individual human approval. Ambiguity, missing media, unknown
dependency changes and failed preservation remain explicit exceptions. A prepared policy patch
or partial publication does not close #155 or #167.

The awake worker prepares renewal inputs after each source batch. Passing proposals proceed through
the actual planner and an additional independent review of final facts, metadata and media. Known
protected facts cannot be downgraded and credentials cannot be approved. Ubuntu replays the complete
private package under the publication coordinator lock, rejects a superseded deployed baseline or
protected-main policy, then prepares the linked catalog issue and PR. Interrupted delivery reuses its
issue, worktree and PR checkpoint and retries auto-merge enablement. Capture, other source reviews and
verified content continue while catalog exceptions or PRs remain pending.

Manual phases use the same private evidence:

```bash
html-to-markdown review-curation --capture captured-source --source-review source-review \
  --output private-renewals --phase all
html-to-markdown review-curation --capture captured-source --source-review source-review \
  --output private-renewals --phase qualify --baseline verified-release \
  --publication-sha256 RECEIPT_SHA256
html-to-markdown review-curation --capture captured-source --source-review source-review \
  --output private-renewals --phase stage --baseline verified-release \
  --publication-sha256 RECEIPT_SHA256 --staging-dir sanitized-catalog
```

`qualify` executes inference only on the MacBook. `stage` is offline and requires preserved final
validation. The normal worker transfers raw evidence privately through SSH; public files contain
only catalog guards, fixed contextual reasons, policy versions and sanitized receipt digests.

Whole-corpus qualification enumerates all baseline article validation gaps and changed
dependencies before promotion. Each gap receives a complete article and native-media
review against every topic, bound to the exact verified baseline receipt. Historical
approval under another topic does not approve the new placement. These reviews retain
the baseline capture age and do not count as fresh source admission. Unavailable media,
unresolved reviews, or an actual planner failure after independent review prevent promotion;
the private dependency inventory and every attempt remain visible for resumption.
