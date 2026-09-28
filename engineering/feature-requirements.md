# Feature requirements

## Sources and access

- Anonymous page retrieval is restricted to HTTPS URLs below `docs.cloud.f5.com/docs-v2` and `my.f5.com/manage/s`, plus `www.f5.com/products/distributed-cloud-services` and its descendants and three exact reviewed solution URLs.
- Every redirect is checked against the same source boundary.
- Authentication walls and HTTP-200 soft 404 pages are explicit failures or removals.
- Meaningful image assets may be retrieved only from the fixed F5 asset-host allowlist.

## Documents

Every output is `content/<sourceId>/<stable-path>/index.md`; its assets are
`assets/<sha256>.<extension>`. Frontmatter uses the order `sourceId`, `title`,
`slug`, `url`, `category`, `publication_date`, `modification_date`,
`content_hash`, `tags`, followed by optional `description`, `subcategory`, and
`breadcrumb`. Missing required dates are YAML `null`. The content hash covers
the normalized body only.

The renderer preserves headings, paragraphs, nested lists, links, tables, fenced code, callouts, figures, and meaningful images. Site navigation, headers, footers, cookie/search/share controls, recommendations, and Return-to-Top sections are removed.

## Source behavior

Docs-cloud discovery expands service navigation trees and adds OpenAPI-derived and in-content links. It detects navigation-only pages, preserves hierarchy, extracts path metadata, and resolves internal redirects.

MyF5 discovery selects the F5 Distributed Cloud product, limits document types
to Support Solution, Operations Guide, Knowledge, and Policy, paginates all
results, waits for Ajax markers, extracts K-number identities, recursively
flattens Shadow DOM, preserves figures in document order, and retains Related
Content.

F5 marketing discovery reads `landing-pages-sitemap.xml` and fails if it is unavailable or malformed. It selects only the Distributed Cloud product root and descendants, then adds the exact Web App and API Protection, Multicloud Networking, and Hybrid Multicloud Application Delivery solution URLs. Extraction keeps substantive `<main>` content and removes navigation and promotional controls. Product and solution paths retain their complete URL path under `content/www-f5-com/`.

## Release gate

Only a complete combined snapshot may ship. A release contains all three source directories, `manifest.json`, and `SHA256SUMS`; the workflow publishes `content-YYYYMMDDTHHMMSSZ` only when document/removal hashes differ from the latest release.
