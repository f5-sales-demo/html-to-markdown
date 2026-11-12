# Versioned enrichment span addresses

Issue #183 supports complete-corpus delivery #155. CommonMark can create several nested
list items at the same heading, kind and starting line. Those spans may have different
ranges or identical bytes. They still require distinct classification and preservation
decisions.

New version-two enrichment artifacts explicitly set `structural_address_version` to 2.
Source and candidate analysis then append deterministic occurrence suffixes to coincident
addresses. Span bytes, line ranges, ordering and digests remain unchanged. Preparation,
section requests, whole-document validation and offline replay use the recorded contract.
Duplicate or missing facts, changed digest guards and lost protected payloads remain
rejections; ancestor/child edits still cannot overlap.

Historical artifacts default to address version 1. Their field serialization, analysis
and request inputs retain the original contract. The new selector is omitted when its
value is 1, preserving historical artifact bytes. Schema-one artifacts cannot select
the new address contract. The content manifest remains schema v2.

## Acceptance

- [x] Reproduce coincident nested spans with both different and identical ranges.
- [x] Keep every span uniquely addressable with exact bytes and digest guards in new artifacts.
- [x] Reject missing/duplicate classification and failed independent fact/payload validation.
- [x] Preserve historical schema-one and schema-two serialization and replay defaults.
- [x] Verify repeated analysis and whole-document candidate checks from identical inputs.
- [x] Pass application and focused publication round-trip checks, static analysis and security checks.
- [ ] Complete current-head CI, normal squash merge and compatible release under #179.
- [ ] Finish the complete-corpus publication and route/reconciliation gates under #155.
