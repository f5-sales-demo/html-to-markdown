# Developing html-to-markdown

## Immutable inputs

Production Pages builds consume exactly one timestamped `content-*` release.
Supply both `snapshot-tag` and the SHA-256 digest of that release's
`publication.json` as workflow inputs. The reusable workflow verifies the
closed release asset set, receipt, source commit, file sizes and digests,
archive checksum, safe member paths and types, `SHA256SUMS`, manifest schema,
document hashes, and asset hashes before extraction.

Never use GitHub's `latest` release or `main` as a content dependency. A
software release such as `v1.1.0` and a timestamped content snapshot are
independent identities.

The Pages caller also pins `docs-builder@sha256:<digest>` and the reusable
docs-control workflow commit. It passes that same full commit SHA as
`snapshot-verifier-ref`, ensuring the checked-out verifier is identical to the
reusable workflow revision. A manual rebuild requires the same immutable
`snapshot-tag` and `publication-sha256` values as an automatic deployment.
Every changed snapshot dispatches the repository's own Pages workflow. The
project site receives the exact protected-main content commit from its caller;
it never resolves a floating release or content branch.

## Local application checks

```bash
uv sync --all-groups --locked
uv run playwright install --only-shell chromium
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=html_to_markdown --cov-fail-under=80
uv run bandit -q -r src
uv run python scripts/audit_dependencies.py
```

CI additionally runs Pylint 4.0.6 on each changed Python file. Use the same
version and repository configuration when checking those files locally:

```bash
uv run --with pylint==4.0.6 pylint --rcfile=.python-lint src/html_to_markdown/media_encoding.py
```

The dependency audit exports all runtime and development dependencies from `uv.lock`
with exact versions and distribution hashes, applies platform markers, and requires
one complete audit result per active dependency. Skipped, missing or vulnerable
dependencies fail the check. The local project is checked by the source security
tools above; it is not queried as a third-party PyPI package.

Verify downloaded release artifacts before inspecting or extracting them:

```bash
gh release download content-YYYYMMDDTHHMMSSZ \
  --repo f5-sales-demo/html-to-markdown \
  --dir snapshot-release
uv run html-to-markdown verify-publication \
  --output snapshot-release \
  --release-tag content-YYYYMMDDTHHMMSSZ
```

The production docs-control verifier additionally binds the receipt's source
commit to the exact release tag before mounting the extracted tree read-only.

## Daily review evidence

Complete unrelated candidates can use source-irrelevance/1. The original relevance
proposal and an independent Sol High validator each receive the complete article,
metadata and every original media frame. The validator must confirm irrelevance,
complete extraction, current policies and exact frame coverage. Missing evidence
or disagreement stays queued. The resulting policy exclusion publishes no files
and grants no privacy approval; its request pins all prior privacy attempts and
correction history. Relevant, lifecycle and retirement cases keep full admission,
and historical full-admission exclusions replay under their original contract.

Completed source decisions can replay across explicitly qualified controller changes.
The packaged source_review_compatibility.json binds the current complete Python
controller digest and each qualified prior controller to its released source commit,
wheel digest and original request context. Every captured source, article, authored
metadata, media, policy and model guard must still match. Replay reconstructs the
complete original logical requests and checks their exact hashes against preserved
responses; native coverage, privacy enforcement, current retirement/API planning and
output digests remain mandatory. Unknown controller pairs and changed core inputs
stay queued. Compatibility never grants an exhausted or pending proposal another
editorial allowance. Preserved source receipts remain immutable; refresh evidence
records the compatibility registry and controller pins when historical inputs are used.

Mac inference synchronizes the actual installed native executor and model-policy
contract through export-inference-executor. Every queued job still requires its exact
executor and request digest. A protected-main processing-code update cannot prevent
an otherwise compatible Mac from draining already bound inference jobs. Ubuntu
source review, policy activation and publication retain their protected-main code
and policy checks. Native or model-permission changes fail the inference handshake.

Fresh complete native frames and source-region inspection views use portable PNG
encoding version 2. The encoder retains the byte-stable version-1 PNG structure,
recompresses only its IDAT scanlines through standard zlib, and preserves every
other chunk. Actual Mac/Ubuntu qualification checks both exact reconstructed
legacy bytes and identical version-2 bytes for the complete supplied fixtures.
The default `png_bytes` API retains version 1 for historical callers; reviewed
privacy transforms explicitly declare `portable-1` or `portable-2` for replay.
Unknown encoding markers fail closed. Captured source media is never rewritten.

Complete SVG observations include verified frame geometry and the original XML
as untrusted metadata. Native review retains the whole rendered frame rather
than thumbnailing it. Safe unchanged vectors retain their original bytes;
reviewed pixel transforms bind the exact source raster and replay its digest
before producing a versioned PNG. External resources and active SVG elements
remain forbidden.

Fresh source records pin media encoding version 2. Historical records without
that marker retain version 1; complete review and replay transactions scope the
encoding separately for each concurrent job. Controller and policy digest checks
continue to reject stale decisions before replay. Native media cache generation 3
carries complete SVG geometry and compressed views. Generation-2 files retain
exact historical views. Cross-queue reuse and isolated replay preserve both
generations, and source-reference repair seeds the matching generation from exact
captured OCR and full frame guards. Cache or encoding
changes grant no new editorial allowance and cannot rebind a model response.

Cross-queue prior selection also applies the exact qualified controller-compatibility
contract before comparing bindings. This stages original requests and evidence for
current complete capture replay; it grants no admission by itself. Unknown controller
pairs and changed source, media, metadata, model or policy guards remain ineligible.
Strongest failed or exhausted privacy histories retain their consumed allowances.
The checkpoint copier resolves the original logical request through the supported
proof schema, including source-input-projection/1 wrappers with nested native
boundaries. It preserves the complete original root and nested checkpoints.
Unknown or malformed proofs remain visible exceptions; copying cannot grant
admission or replace independent replay.

The Mac worker seeds eligible source batches from prior private capture queues.
Unchanged bound article, metadata, media and policy/request inputs reuse exact
model evidence, then replay against the fresh raw capture receipt before gaining
an accepted or excluded disposition. Changed inputs follow normal review. Prior
records and native-media caches remain immutable; exhausted and pending privacy
ledgers carry across queues. Unsafe or unreadable prior evidence remains queued
for repair. Interrupted storage copies resume before any new privacy attempt.
`cross-queue-reuse.json` and its evidence/errors are private operational artifacts.

Retained baseline privacy prerequisites share their strongest exact-source editorial
history across curation queues. Before inference, the worker verifies original
article/media custody and reconciles current records plus immutable histories, including
queues whose current record was retired, under the existing orchestrator and per-source
locks. Conflicting strongest proposal histories stay queued; hash order cannot select
between divergent exhausted ledgers. Reconciliation preserves the original
proposal and all failed feedback; another queue or controller change grants no new
editor allowance. Final replay rejects weaker or altered ledgers. Standalone
qualification can set `HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT` to the same private
worker root; normal processing sets it from its output directory.

Baseline and source-reference replay reconstruct the complete validated logical
source request before checking text, metadata, original media, or privacy feedback.
The physical source-input-projection/1 request is retained exactly as transport
evidence. Independent reference validators receive a lossless projected source
representation with explicit reference-resolution instructions when needed to fit
the native character limit; all original source values and media remain available.
Unresolved proposals and consumed correction allowances remain unchanged.

Baseline review transactions register each exact history generation when its owning
writer preserves it, including intermediate native-media corrections and canonical
proposal renewal. Ownership is scoped to the current worker and destination; nested
candidate outputs and other threads cannot authorize foreign additions. Registration
does not grant relevance, privacy or publication approval. Private final failures
record unexpected roots and history paths without changing the observed custody.

Baseline review batches share a bounded in-memory history lookup. It parses each
unique digest-verified history once, retaining only five source identity strings.
Every duplicate copy is still independently hashed before lookup reuse. Every
lookup enumerates custody again, discovers newly added histories, and fully checks
matching records against original source/proposal/budget evidence. Before returning
a batch, the controller streams and hashes every indexed history again and rejects
unobserved additions, removals, symlinks or changed bytes. Filesystem timestamps
never grant approval. Failed final integrity verification queues the whole batch;
saved responses and exhausted allowances remain intact. The private batch report
records parsing and hashing byte counts separately from article eligibility.

Conflicting strongest baseline histories have a separate private resolution journal.
Every original proposal, request and consumed correction remains unchanged. Only an
already completed strongest proposal can undergo current complete source, native
media, privacy, credential and preservation validation. An independent Sol High
selection must disposition every strongest record; distinct validated outputs remain
ambiguous. The selected source keeps its full original allowance and still requires
retirement/API curation. Missing or changed source bytes, histories, policies,
responses or controller context invalidate the selection. No qualifying existing
proposal leaves an explicit exception for an independently admitted current source
replacement; it does not grant another editor attempt.

Retained native OCR replays from the complete digest-checked original relevance
request. Every original image byte, source digest, full frame, geometry, duration,
loop and metadata is independently verified against the archived media. Re-running
OCR or changing a shared OCR cache cannot alter a saved model request. OCR remains
untrusted context; exact transformations, current independent privacy validation and
credential enforcement remain mandatory. Replay does not repair cached inputs.

Grouped governed policy and planner replay share the bounded history lookup and
verify all consulted custody before returning. Planner receipts are written only
after that final verification succeeds. Standalone verification retains exhaustive
discovery, and selected-conflict replay changes no preserved input files.

Source workers select a current review frontier before each sweep. The default
`list-review-queues` still enumerates all captured queues; `--review-frontier`
prioritizes capture epochs and defers an older queue only when strictly newer
selected inventories cover every identity and every completed or terminal capture
outcome in that source. Merely discovering an identity whose capture is pending
cannot hide a complete older capture. Current access and media failures remain
terminal capture outcomes; they grant no admission. Older inventories with only pending
captures can defer without losing captured material. Missing identities, unknown epochs,
tied epochs and enrichment work remain visible. Private
`review-frontier.private.json` pins both discovery and capture-outcome coverage;
deferral does not grant a policy exclusion.
All original inventories, decisions and strongest privacy histories remain
available for cross-queue reuse and final replay.

Known publication prerequisites can use a private `review-priorities.private.json`
in the Ubuntu queue root. Its strict schema is `{"schema_version":1,"identities":[],"urls":[]}`;
values are stable source identities or canonical public F5 source URLs. A worker
reads and records one priority-file digest per sweep. Evidence reuse and bounded
source review use the same selector: explicit prerequisites, previous publication,
then URL order. Unselected candidates remain queued. Priority never changes
discovery origins, capture bytes, admission checks, model inputs or privacy budgets.
Changing this scheduling file alone does not invalidate bound approvals.
Standalone bounded `review-content` can use `--priority-input PATH` together with
`--priority-sha256 DIGEST`. Missing worker configuration preserves default ordering.

Source review and curation run in separate scheduling passes. Each bounded source
batch is sealed, independently returned and checkpointed before the next source
queue advances. Governed publication preparation follows the source sweep, before
curation dependency reviews. A blocked publication keeps its policy gate and private
diagnostic while the prerequisite reviews continue. Settled source checkpoints still
participate in the curation pass, so interrupted dependencies resume without repeating
acknowledged source returns or changing request hashes or privacy ledgers.

Ubuntu source returns share the sealed snapshot's immutable file inodes rather than
packing and extracting another full capture archive. Original, staged and promoted
inputs independently replay the same receipt. A guarded private transaction resumes
interrupted filesystem work, rejects conflicting or unsafe members, and serializes
promotion with archive receivers and publication. Payload sharing preserves capture
ages and model/decision hashes; original source files and failed attempts remain
private. Remote source returns keep the complete archive verification protocol.

Baseline privacy dependencies in the production curation pass advance in cohorts of
at most eight complete articles. Each cohort closes its inference client and verifies
the complete indexed history before atomically advancing a private scheduling cursor.
Interrupted cohorts resume from the same cursor; failed article reviews cannot starve
later identities. Scheduling changes never grant approval or reset editorial allowances.
Changed inventories preserve the prior scheduling checkpoint and begin a fresh traversal.
Unresolved prerequisites report `dependency_review_pending` and stop planner validation,
policy transfer and publication. Exhaustive standalone review and offline replay retain
their existing behavior; final policy qualification still checks every dependency.

Retained dependency replacement lookups first inspect the exact digest-pinned source
receipt. A missing or non-accepted matching entry returns no replacement without
replaying the complete source bundle; it grants no exclusion, privacy or publication
approval. Receipt digest, source identity and receipt-path safety checks still apply
to negative lookups. Every possible accepted replacement retains complete source
replay, URL and capture freshness checks, original baseline custody and native-media
verification. Standalone source and final publication verification remain exhaustive.

Curation also preserves the complete original content corpus in private
`.curation-topic-inputs` evidence when later topics change a reviewed retirement output.
The ledger binds original metadata, bodies, media, dependency state and exact
policy pins. Verification independently replays all topic transformations before
checking API projection and grounded final metadata. The audit registers the
ledger digest; missing, altered or stale records remain exceptions. Historical
API-only evidence retains its original replay contract. Original source and
privacy approvals remain required, and these private inputs never enter consumer
archives. Repeated verification shares one scoped replay and checks the inputs
and final output again before the scope ends.

## Documentation build

### Closed private evidence relocation

Linux operators can prepare an exact private copy with
`scripts/relocate_closed_evidence.py`. Supply absolute, disjoint source,
destination and evidence directories, plus the same `--coordination-root` for
every transaction sharing storage. The global lock covers copying, complete
byte and metadata verification, persistence and redundant-backend retirement.
The tool rejects symlinks, unexpected members, writable handles, unknown process
access, conflicting destinations and changed transaction inputs. It preserves
hardlink groups inside the selected tree and reports cross-root exclusive inode
allocation separately from actual recovered filesystem capacity.

```bash
python scripts/relocate_closed_evidence.py \
  --source /data/private/closed-source \
  --destination /storage/private/closed-copy \
  --evidence /storage/private/relocation-receipts \
  --coordination-root /storage/private/coordination
```

The default leaves the original namespace intact. Repeating the exact invocation
resumes its owned copy; `--cutover` additionally binds the independently verified
copy at the original path, persists the mount and installs a processor mountpoint
prerequisite. Only then does it reverify and retire redundant backing files.
Interrupted binding and retirement resume from durable intent and manifest pins.
No source or inference worker is signaled, no provider or credential is configured,
and no model response, editorial allowance or admission decision is changed.
Detailed failures, manifests and subprocess output remain private; stdout contains
aggregate operational results and grants no publication approval.

Qualify actual filesystem behavior before production adoption:

```bash
python scripts/qualify_evidence_relocation.py \
  --workspace /storage/private/new-relocation-fixture
```

This requires noninteractive sudo, Btrfs tools and a new private workspace.
It exercises normal relocation, interruption after binding, interrupted retirement,
complete bytes, ownership, private modes, nanosecond timestamps, hardlink groups
and an original open read handle on an isolated compressed loop filesystem.
Mount configuration uses fixture files; production fstab and processor units are
untouched. Keep the closed image, receipts and any failed attempt as private evidence.
Compressed filesystems can report logical `st_blocks`; use filesystem allocation
and free-space measurements when reporting physical savings. Source retirement
still requires complete independent byte verification and an available mounted copy.

Mount the one-page `docs/` directory and a verified extracted corpus
separately:

```bash
docker run --rm --pull=never \
  -e DOCS_BASE=/html-to-markdown \
  -e DOCS_SITE=https://f5-sales-demo.github.io \
  -e MACHINE_CORPUS_DIR=/content/machine-corpus \
  -v "$PWD/docs:/content/docs:ro" \
  -v "$PWD/verified-corpus:/content/machine-corpus:ro" \
  -v "$PWD/output:/output" \
  ghcr.io/f5-sales-demo/docs-builder@sha256:<digest>
```

The corpus must never be placed under `docs/` or `src/content/docs`.

## Route hierarchy

- `/html-to-markdown/` is the only human content route.
- `/html-to-markdown/llms.txt` links to one index per source and includes source roots.
- `/html-to-markdown/llms-full.txt` is an exhaustive grouped link manifest; it never embeds
  document bodies.
- `/html-to-markdown/_llms-txt/<source>/<path>.txt` contains metadata-only intermediate
  category and shared-subcategory indices plus full Markdown at canonical leaf routes.
- `/html-to-markdown/snapshot/` exposes the verified manifest, checksums, quality reports,
  provenance, Markdown, and content-addressed assets.

Progressive mode is English-only and does not publish locale routes,
`llms-small.txt`, a context-dump endpoint, or scraped-document HTML pages.
Repository `docs/llms-config.json` selects the authoritative `category` then
`subcategory` taxonomy, collapses missing or singleton subcategories, and caps
plain first-sentence hints at 240 characters. The builder consumes this file
before Starlight renders content.

## Content policy and API migration

`src/html_to_markdown/content_policy.json` is separate from metadata classification.
It excludes the two exact retired documentation path families, pins the compact
compressed destination catalog by SHA-256, and supports explicit URL overrides. The catalog
records the immutable specification release and archive digest, domain schema
digests, captured route inventory digest, and successful destination qualification.
Operation destinations use the published canonical routes without a locale prefix.

Normal runs apply the policy to discovery, benchmark and previous-release seeds,
resumed state, canonical redirects, reconciliation, and packaging. Retained content
is migrated after extraction and privacy review, including carried-forward pages.
Metadata descriptions, internal relationships, body and file hashes, checksums,
and quality reports are rebuilt. External API references remain external; original
source URLs and freshness provenance remain attached to retained documents.

Examine or migrate a verified extracted snapshot entirely offline:

```bash
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown migrate-content --output verified-corpus
```

Resolution uses an explicit override, exact operation identity from a path or
fragment, then exact resource identity with hyphen/underscore normalization.
Unmatched and ambiguous references use the catalog fallback and retain candidates
and reasons. Resource matches link to a domain overview and append a deduplicated
Related API reference section. Markdown, reference definitions, HTML, bare URLs,
and documentation URLs in code examples are rewritten; REST request paths remain.

Migration and topic evidence lives in `curation-audit.json`, a separate CI artifact.
It is excluded from archives, release assets and Pages. Consumer quality reports
measure retained content only. The manifest remains schema v2.

`curation_policy.json` declares independent versioned topics, digest-pinned evidence,
exact retired identities and URLs, reviewed source decisions, structural block
removals and media decisions. The SMSv2 topic uses reviewed source guards and
omits unclassified candidates or stale/overlapping removals. The separate
AppStack topic uses exact aliases, retired site/API identities, reviewed
document edits, and page-scoped media digests. It rechecks new, changed,
carried-forward, and restored pages before Terraform curation. The Terraform
topic runs automatically on new, changed, carried-forward and restored pages.
It removes retired provider instructions and their dependent material, retains
independent explanations and cloud infrastructure, and adds only qualified xcsh
links. See `engineering/smsv2-curation.md`,
`engineering/appstack-curation.md`, and `engineering/terraform-curation.md`
for the evidence and ownership boundaries.

```bash
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown curate-content --output verified-corpus
```

Both normal runs and offline curation inspect authored legacy classifications before SMSv2, AppStack, vesctl retirement, Terraform, and
API-reference migration through one planner. See `engineering/legacy-curation.md` for the collection-wide source review. Examination does not
change content and reports the same proposed transformations with stage digests. Normal and offline
reapplication are stable across metadata, reports, manifest, checksums, archive
and audit. Updating pinned evidence or decisions requires a versioned PR.

Catalog updates require a versioned pull request. Download an immutable enriched
specification archive, verify its GitHub asset digest, capture the published sitemap,
then qualify every promoted operation, overview and fallback before promotion:

```bash
uv run python scripts/qualify_api_catalog.py \
  --spec-archive f5xc-api-specs-v10.0.1.zip \
  --spec-sha256 c7ed97855749306619296dbf125e510298f859098fa6ed84c34bca39537019ec \
  --routes sitemap-0.xml \
  --routes-sha256 b5491b6aa0afcf1c768c7f4f716105db2a1c322a8fc2e6c3e543d0bbfd077cd5 \
  --release-tag v10.0.1 --output candidate-catalog.json.gz
```

Qualification requires HTTP 200, the expected canonical URL, rendered method/path,
and the specification's source operation link. Any failure prevents output promotion.
Commit the qualified catalog and its updated policy digest together. Runtime migration
performs no network discovery or model inference. Previously published snapshots
remain immutable; publish migrated content under a new receipt-pinned snapshot tag.

## Curated collection landing page

The landing page explains source-to-Markdown conversion and curation. Its hero
introduces the documentation collection without exposing conceptual design labels
or adding sections to fill a template. `CorpusBrowser` comes from the pinned docs-builder and derives its
source cards, topic cards, document summaries, breadcrumbs and search from the
verified mounted snapshot. It never maintains a separate manual page inventory.

Every card has a canonical Markdown destination. The browser supports ordinary
index links without JavaScript and progressively narrower cards with JavaScript.
Captured Markdown and assets remain unchanged; display summaries omit capture
dates and invisible text. Curation is an extensible collection feature, not a
showcase for any individual policy.

## Ubuntu processing and bounded Mac inference

Keep development, source captures, media processing, review journals, curation,
enrichment, replay and packaging on Ubuntu. Configure a private absolute inference
queue for Ubuntu's `processing-worker` or install its user service with
`install-processing-service`. Ubuntu never invokes a local model.

A complete capture import verifies every declared transfer payload before writing
its members. Local Ubuntu imports reuse the collector's immutable capture store
when it shares the destination filesystem; other imports retain their separate
worker store. Reuse verifies cached bytes, ownership, permissions and safe paths
before linking the original capture filenames. Mutable inventory and review state
keep their own files.

The import groups capture-store directory syncs in an independent scope. New
payload bytes are still synced and digest-checked individually; each affected
directory is flushed before import success permits its capture checkpoint.
Standalone writes retain immediate directory durability. A failed final directory
sync rejects readiness, preserves completed input bytes and resumes through the
ordinary transfer and cache checks. This changes persistence work, while source
admission, privacy, retirement and publication continue through their own gates.

On the Mac, `stage-inference-agent` prepares the 15-minute `mac-inference-worker`
launch definition. `activate-inference-agent` waits for real worker and inference
quiescence before replacing the legacy full-source worker. The login shell retains
the existing configured Codex/LiteLLM Responses credential route; no credentials
or provider overrides belong in the launch definition.

The Mac claims at most four current requests and their media. It preserves each
original request, raw attempt and completion, returns an immutable transfer
archive, and removes local spools only after Ubuntu independently verifies all
evidence bytes. A lost acknowledgment resumes the same archive and job without
another model call. A failed source or preservation check remains queued.
The monitor returns each completed job through independent Ubuntu verification
while slower peers continue. It refills freed slots from ready follow-up work
with the same four-job bound. A declared new-claim limit accounts for receipts
that commit before a local future observes its acknowledgment. Owned jobs resume
under their original owner and executor. After completed work, the monitor remains
available for up to 15 quiet minutes for Ubuntu's dependent phases, reporting
`waiting_for_followup`. Empty polls run no inference. An initially empty queue
returns immediately to the existing 15-minute launch clock. Queue heartbeats
continue across batches; pending returns stop draining and retain their exact
archives for the next recovery attempt.

Historical evidence migration must verify the complete restored Ubuntu tree and
the unchanged Mac source before removing a local duplicate. Keep restoration
receipts and original-path mappings private. Do not remove files used by live
workers, infer quiescence from a lock file alone, or restart a job just because
observation timed out.

## Retained dependency dispositions

Retained baseline review separates source relevance/extraction from privacy eligibility.
A complete irrelevant article needs independent complete-span and native-frame validation
before `source_exclusion_validated` can enter the governed catalog. Its guarded decision
uses `review_kind=source_relevance_exclusion` and a fixed safe reason; it does not classify
that article as retired or expose original content. The actual planner receives a separate
independent exclusion check before the policy can be staged. Changed original text, authored
metadata, media, policy or request evidence keeps the decision queued.

Relevant malformed retained articles stay in the source/extraction queue. Only an exact,
independently admitted current source can replace their prerequisite. Dependency selection
uses acknowledged source checkpoint pins, preserves the complete admission chain and the
original baseline custody, and never grants another historical privacy edit. The combiner
selects verified replacements and exclusions before validating the remaining baseline, then
replays selection and whole-corpus curation independently. All kept articles retain privacy,
retirement/API and preservation checks. Reviewable dependencies progress while unresolved
ones remain in the dependency inventory and exception report.
