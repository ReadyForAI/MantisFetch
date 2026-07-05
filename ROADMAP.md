# MantisFetch Roadmap

## ✅ Completed

### Foundation
- [x] Unified server entry point — browser (`/web`) and docreader (`/doc`) on a single port (9898)
- [x] Project packaging — `requirements.txt`, `pyproject.toml`, ruff + pytest config
- [x] Test framework — pytest with health endpoint tests, TestClient fixtures

### Core Features
- [x] One-shot web capture — `POST /web/capture` persists a URL to the document library in a single call
- [x] XLSX / CSV parsing — spreadsheets and CSVs join PDF and DOCX as first-class document types
- [x] Multi-LLM provider abstraction — swap between Gemini, OpenAI-compatible APIs, and local Ollama via env var

### Distribution
- [x] Docker Compose — `docker compose up` runs the full stack with a single command
- [x] README and Contributing guide — quick-start, API overview, dev setup, PR process

### SDK & Ecosystem
- [x] Python SDK — sync (`MantisFetchClient`) and async (`AsyncMantisFetchClient`) clients with full API coverage

### Validation
- [x] E2E web capture tests — full pipeline: capture → digest → sections
- [x] E2E document parse tests — PDF, DOCX, XLSX, CSV; four-step flow per format
- [x] E2E cross-source search tests — keyword, `file_type`, and tag filters across WEB and DOC sources
- [x] E2E SDK round-trip tests — sync and async clients exercised against a live server
- [x] Full pipeline smoke test — single `test_full_pipeline()` covering all components with a pass/fail summary table

### Housekeeping
- [x] Default docs directory moved to `~/.mantisfetch/docs`, configurable via `MANTISFETCH_DOCS_DIR`

---

## 🔜 Next

- [ ] JavaScript / TypeScript SDK (`npm install mantisfetch-client`)
- [x] Optional API key authentication (`Authorization: Bearer <token>`) — `MANTISFETCH_MCP_TOKEN` gates `/web`, `/doc` and `/mcp`
- [ ] Streaming progress events for long parse and capture operations
- [ ] GitHub Actions CI pipeline (lint, unit tests, build Docker image)
- [ ] PyPI package publishing (`pip install mantisfetch`)

---

## 🔭 Future

- [x] MCP server — expose the `/web` browsing loop **and** `/doc` document library as [Model Context Protocol](https://modelcontextprotocol.io) tools for agents (streamable-HTTP at `/mcp`; per IRP with NodalOS)
- [ ] Semantic / vector search — embedding-based retrieval alongside keyword search
- [ ] Additional LLM providers — Anthropic Claude, built-in Ollama auto-detection
- [ ] Plugin system — register custom parsers for proprietary document formats
- [ ] Web UI — browser-based document library explorer
- [ ] Incremental re-capture — re-fetch and diff a previously captured URL

---

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) to get started.
