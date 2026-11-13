# Private content collection and review

Collection, admission, enrichment and deployment have separate receipts. An inventory is not a publishable corpus. Every
captured article stays queued until the independent admission and replay checks pass. Failed enrichment retains the
complete admitted article; failed source admission retains the verified published article or queues a new candidate.

## Capture and admission

Run collectors on Ubuntu with a receipt-verified deployed baseline. Store captures under a private user directory,
separate from Git and consumer artifacts:

```sh
html-to-markdown inventory-content --source all --output PRIVATE_CAPTURE \
  --previous-manifest VERIFIED_BASELINE/manifest.json --full
html-to-markdown inventory-content --source all --output PRIVATE_CAPTURE \
  --previous-manifest VERIFIED_BASELINE/manifest.json --full --resume
```

SQLite preserves discovery events and completed records during interruptions. Enumeration, capture, review and
publication completeness are independent. Unavailable articles and media remain explicit exceptions.
`repair-captured-extraction` re-renders preserved source bytes into immutable extraction paths and retains the previous
extraction; it does not fetch or approve content.
Repair history is cumulative, and repeating an unchanged repair preserves the stored inventory bytes. Literal
angle-bracket text and ordinary capability-card boundaries remain visible after Markdown rendering; code payloads
retain their exact strings.

On the MacBook, prepare and review transferred captures:

```sh
html-to-markdown review-content --capture PRIVATE_CAPTURE --output PRIVATE_REVIEW --phase prepare
html-to-markdown review-content --capture PRIVATE_CAPTURE --output PRIVATE_REVIEW \
  --phase all --journal PRIVATE_JOURNAL
html-to-markdown review-content --capture PRIVATE_CAPTURE --output PRIVATE_REVIEW --phase replay
```

Workers use the installed Codex CLI and its existing LiteLLM Responses route. Classification and proposals use
`gpt-6.1-sol`; routine independent validation uses a separate `gpt-6.1-sol` worker at high reasoning, with no execution, browsing, connector
or collaboration tools. The controller handles capture, OCR and exact transformations. Complete requests, native media
frames, attempts and private transformation evidence remain available for replay. Changed source, media, policy or
controller context invalidates admission. Credential findings cannot be waived.
Model policy two binds editor and validator roles separately even when both use Sol. Different role/request hashes
prevent the validator from reusing the proposer response. Astra escalation requires an exact completed Sol validation
with specific unresolved privacy or preservation findings, an explicit reason and preserved prior evidence. Unknown
policy versions, wrong roles and ungrounded escalation fail closed. Historical Astra receipts replay under their
original model and request contracts; new uncached Astra requests require a verified exception.
Independent relevance validation approves or rejects the proposed inventory disposition, including justified
exclusions. An approved exclusion emits no article. Privacy or media rejection returns specific independent findings
for the bounded correction loop; all mandatory checks still have to pass.

When a privacy proposal lacks guards for visible identifying values missed by OCR, the controller requests native-frame
observations and independent confirmation. Readable values receive literal digest guards. Clipped or unreadable values
receive guards for their exact media, frame and rectangle without invented hidden text. Those guards support precisely
placed image labels; they do not authorize redaction or serve as text-edit guards. The final validator still checks the
complete source, transformed article, every frame, technical relationships and credential enforcement. Private feedback
can request additional grounded observations within the existing two-correction limit. Failed or stale observations and
all earlier privacy attempts remain preserved; unresolved regions stay queued.

## Queue clocks and activation

Install the qualified software into stable user-owned paths before installing either scheduler. Temporary development
worktrees must not be the long-term executable dependency.

Ubuntu user timers preserve the daily official-doc clock at 04:23 UTC, collect other sources daily at 04:43 UTC, and
perform full Sunday reconciliation at 03:00 UTC:

```sh
html-to-markdown scheduled-collection --queue-root PRIVATE_QUEUE --install
```

The Mac launch agent checks every 900 seconds while awake. Its worker and inference locks prevent overlap, and inference
concurrency is four. Configuration stores executable paths and queue arguments, with authentication supplied by the
existing credential route:

```sh
html-to-markdown review-worker --install --host VERIFIED_SSH_ROUTE \
  --remote-executable STABLE_UBUNTU_EXECUTABLE --remote-queue PRIVATE_QUEUE \
  --output PRIVATE_MAC_WORKER --transport VERIFIED_SAFE_SSH_EXEC \
  --source-commit QUALIFIED_PROTECTED_MAIN_COMMIT
```

Each sweep reviews at most eight articles per source, prioritizing previously published articles, and at most eight
enrichment documents per prepared job. Partial admission returns preserve the complete queue and independently verified
passing articles. A new batch does not alter earlier sealed inputs. Interrupted transfers resume before the return
checkpoint advances. Publication selects the most complete reviewed batch for a capture when capture dates match.

The monitor checks Ubuntu queues every 900 seconds during active inference as well as between sweeps. Its private
heartbeat distinguishes an active worker from a failed queue check. Pending counts include unattempted articles and
transient transport failures; unchanged semantic and malformed-response failures stay in the exception report until
source or policy context changes. Invalid privacy proposals receive at most two corrections with exact validation
feedback. A settled queue may still contain exceptions and does not establish complete discovery or publication.

After timer status, the credential route and queue round trips are verified, set the repository variable
`PRIVATE_CONTENT_QUEUE_ENABLED=true`. This suppresses the legacy GitHub collection/publication jobs while retaining
their original clocks for rollback. The CLI commands and official-doc combiner remain compatible. Sleep, transport
errors and unresolved reviews delay new decisions; verified content continues to serve.

## Publication and evidence

The coordinator assembles admitted sources against the latest deployed immutable receipt and prepares version-two
enrichment for the complete resulting corpus. It preserves the selected source inputs, privacy transformations,
retirement/API curation and pre-enrichment corpus. Independent replay verifies each stage and final metadata/assets
separately. Unattempted enrichment documents prevent staging.

Admission preserves the complete privacy-reviewed source separately from the actual retirement/API planner preview.
Independent validation binds both stages through the exact request and curation digests. Privacy and extraction checks
compare the source and privacy candidate; retirement checks inspect the curated article and its retained media. Original
blank fields remain blank unless independently verified source repair evidence supplies their values. Missing or stale
catalog decisions and independent retirement failures enter `curation-exceptions.json` for the linked catalog-renewal
workflow. Planner omissions do not become policy exclusions. A successful replay clears its current exception while
preserving the earlier private failure evidence. The complete combiner still rechecks policy and dependency closure
against the latest verified baseline before publication.

Private returns are checkpointed only after verification. An interrupted enrichment promotion resumes its exact input
pin. Superseded jobs remain private and cannot overwrite a newer baseline. The publication workflow uses the existing
`content-snapshot` lock, verifies the downloaded draft and holds the lock until the exact receipt is deployed.
Stale or invalid source inputs remain in `source-input-exceptions.json` while fresh verified inputs progress. They are
not treated as policy exclusions or reused as approvals. Offline document replay copies only the original and resulting
media analysis needed by that article and checks the complete accepted article/media inventory.

`sanitize-inventory` emits allowlisted CI receipts with dispositions, counts, source routes and digests. Raw captures,
OCR, model responses, unreviewed Markdown, SQLite state and identifying transformation evidence are excluded from CI
uploads, releases and Pages. Sanitized authenticated CI artifacts retain 90 days. Consumer archives contain published
Markdown, necessary media and safe provenance.

Source curation catalog changes still require linked governed pull requests. A passing source admission does not waive
retirement catalog guards or resolve an ambiguous policy decision. Full delivery requires reconciliation of every
candidate, full-corpus enrichment and independent validation, immutable release/deployment, and
canonical/search/alias/anchor/media/removed-route acceptance.
