# Fresh official documentation

The daily **Inventory official documentation** workflow discovers the current F5
Distributed Cloud route inventory from `__NEXT_DATA__.props.pageProps.nav`, then
fetches all current and previously published official-documentation URLs. It does
not depend on the old benchmark URL list or Community relevance/privacy review.
An invalid route inventory or incomplete browser fallback fails visibly.

```bash
uv run html-to-markdown inventory-docs --output docs-inventory \
  --previous-manifest verified-baseline/manifest.json
```

Use a freshly verified snapshot as the baseline and a new directory for each new
pass. Interrupted passes can resume in their existing directory; successful pages
are fetched again, and in-flight pages become pending. Source modification dates,
response hashes and capture times distinguish fresh captures from retained fallback
content. The inventory reports new, changed, unchanged, unavailable, fallback and
policy-excluded pages, including exclusions that remove state during curation.

Every run preserves `inventory.json`, its readable report, source HTML, assets and
state outside consumer Markdown. Changed content invalidates any prior article
enrichment decision; this command performs no inference or stale replay.

`combine-content` validates an exact prior publication receipt and curates every
fresh article independently. A failed article retains its whole verified prior
body and media, with a reason in `refresh-state.json`; a new unresolved article
stays in the queue. Other sources retain their verified published content and
original source-success times. No new Community article or image can enter through
this route. Current retirement, metadata and reference checks still apply.

The daily workflow publishes a changed combined snapshot and dispatches Pages with
its exact receipt digest. `verify-refresh` replays source curation and verifies
every retained body, description and asset against its selected upstream. The
prior aliases survive while search remains canonical. The weekly full-source
workflow separately requires all current Community reviews, and preserves the
inspectable `review-queue.json` when that review blocks publication.
