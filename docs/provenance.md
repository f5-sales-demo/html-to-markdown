# Prototype provenance

The prototypes remain active and unmodified. This repository is a clean rewrite of observed behavior at these immutable commits:

- `robinmordasiewicz/docs.cloud.f5.com@d0aa25d2f6460eca32b132c758709d8e7c3dae07`
- `robinmordasiewicz/my.f5.com@65572c581d560cf3e360e8b93a27fa70ac596cfb`

| Prototype behavior | New module | Verification |
| --- | --- | --- |
| Service links, navigation tree expansion, OpenAPI names | `adapters/docs_cloud.py` | adapter fixture/unit tests |
| Navigation-only detection and hierarchical metadata | `adapters/docs_cloud.py` | navigation and metadata tests |
| Internal redirect resolution | `pipeline.py` | local integration tests |
| Product/document filters, pagination, K-number discovery | `adapters/my_f5.py` | discovery contract tests |
| AJAX markers and recursive Shadow DOM traversal | `adapters/my_f5.py`, `fetcher.py` | script and rendered fixture tests |
| Figure placement, images, Related Content | `render.py`, `adapters/my_f5.py` | golden Markdown tests |
| Retry and resumable status | `fetcher.py`, `state.py` | retry and state-transition tests |
| Frontmatter and content hashing | `models.py`, `render.py` | normalization and YAML tests |

The rewrite intentionally replaces prototype XML/file state with SQLite, synchronous one-page browser ownership with shared async Chromium and isolated contexts, scrape-time date fallbacks with required `null` dates, and per-site output layouts with one deterministic contract.
