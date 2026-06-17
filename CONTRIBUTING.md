# Contributing to MantisFetch

Thank you for your interest in contributing to MantisFetch! This guide covers everything you need to get started.

## Table of Contents

- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Code Conventions](#code-conventions)
- [Running Tests](#running-tests)
- [Pull Request Process](#pull-request-process)
- [Issue Templates](#issue-templates)
- [Code of Conduct](#code-of-conduct)

---

## Development Setup

**Requirements:** Python 3.11+, git

```bash
# 1. Fork and clone
git clone https://github.com/<your-fork>/MantisFetch.git
cd MantisFetch

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
pip install ruff pytest       # dev tools

# 4. Install Playwright browsers
playwright install chromium

# 5. Set your LLM API key
export GEMINI_API_KEY=your_key_here   # or MANTISFETCH_LLM_API_KEY for OpenAI-compat

# 6. Start the server
python mantisfetch_server.py   # http://localhost:9898
```

### Docker alternative

If you prefer a containerised dev loop:

```bash
cp .env.example .env   # populate GEMINI_API_KEY
docker compose up --build
```

---

## Project Structure

```
MantisFetch/
├── mantisfetch_server.py          # Unified entry point (mounts /web and /doc)
├── services/
│   ├── browser/
│   │   └── mantisfetch_browser.py # Playwright web capture & distillation
│   └── docreader/
│       └── mantisfetch_docreader.py # PDF/DOCX/XLSX/CSV parser
├── providers/                   # LLM provider abstraction
│   ├── base.py                  # Abstract LLMProvider
│   ├── gemini.py                # Google Gemini backend
│   └── openai_compat.py         # Any OpenAI-compatible REST API
├── i18n.py                      # Internationalisation (en/zh)
├── tests/                       # pytest test suite
├── skills/                      # Agent SKILL files (API contracts)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

Key design decisions are documented in `CLAUDE.md` and `docs/mantisfetch_opensource_design.md`.

---

## Code Conventions

MantisFetch follows the conventions in `CLAUDE.md`. The highlights:

| Rule | Detail |
|---|---|
| Language | All code, comments, logs, and error messages in **English** |
| Type hints | Required on all **public** functions |
| Docstrings | English, on all public APIs |
| Formatter/Linter | `ruff` (config in `pyproject.toml`) |
| Python version | 3.11+ (use `match`, `tomllib`, `datetime.UTC`, etc.) |
| Commit format | `{type}({scope}): {description}` |

### Commit types

| Type | When to use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Refactoring without behaviour change |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `ci` | CI/CD pipeline changes |
| `chore` | Dependency bumps, tooling |

### Naming

- Files and modules: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private helpers: prefix with `_`

---

## Running Tests

```bash
# Run the full suite
pytest tests/ -v

# Run a single file
pytest tests/test_health.py -v

# Lint check
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Format
ruff format .
```

All tests must pass and `ruff check .` must be clean before opening a PR.

### Writing tests

- Place tests in `tests/test_<feature>.py`
- Use the shared `client` fixture from `tests/conftest.py` for HTTP tests
- Mock external services (Playwright, Gemini, httpx) — tests must run without network access
- Session-scoped fixtures are preferred for expensive setup

---

## Pull Request Process

1. **Branch** off the relevant base branch (usually `main` after merges, or a task branch for chained work):

   ```bash
   git checkout -b feat/my-feature
   ```

2. **Implement** your change following the code conventions above.

3. **Test** — make sure `pytest tests/ -v` and `ruff check .` both pass.

4. **Push** and open a PR against `main` (or the appropriate base branch):

   ```bash
   git push -u origin feat/my-feature
   gh pr create --title "feat(scope): short description"
   ```

5. **PR description** should include:
   - What changed and why
   - How to test / reproduce
   - Any breaking changes

6. A maintainer will review within a few days. Address feedback by pushing additional commits (no force-push on open PRs).

7. PRs are squash-merged into `main` once approved and CI is green.

### PR checklist

- [ ] `ruff check .` passes
- [ ] `pytest tests/ -v` passes
- [ ] New public functions have type hints and docstrings
- [ ] No hard-coded secrets or API keys
- [ ] README / SKILL files updated if the API contract changed

---

## Issue Templates

When filing an issue, please include:

### Bug report

```
**Describe the bug**
A clear description of what went wrong.

**To reproduce**
Steps to reproduce the behaviour.

**Expected behaviour**
What you expected to happen.

**Environment**
- OS:
- Python version:
- MantisFetch version / commit:
- LLM provider:
```

### Feature request

```
**Problem statement**
What problem does this feature solve?

**Proposed solution**
How you envision it working.

**Alternatives considered**
Other approaches you thought about.
```

---

## Code of Conduct

MantisFetch is an open, welcoming project. We expect all contributors to:

- Be respectful and constructive in all communications
- Welcome newcomers and help them get started
- Focus criticism on ideas, not people
- Follow the [Contributor Covenant](https://www.contributor-covenant.org/) v2.1

Violations can be reported to opensource@readyfor.ai.

---

Thank you for helping make MantisFetch better!
