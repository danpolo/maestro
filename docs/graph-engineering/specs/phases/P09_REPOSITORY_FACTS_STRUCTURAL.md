# PHASE P9: ADD REPOSITORY FACTS & STRUCTURAL CONTEXT (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P09
Depends On:      P04, P05 (routed context after P06)
Output:          Repository query API, Tree-sitter provider, ContextManifest generator
Target Files:
  - Create:  maestro/repository/queries.py
  - Create:  maestro/repository/index.py
  - Create:  maestro/repository/providers.py
  - Create:  maestro/repository/context.py
  - Create:  tests/repository/test_queries.py
  - Create:  tests/repository/test_overlays.py
  - Create:  tests/repository/test_context.py
Verification:    .venv/bin/python -m pytest -q tests/repository
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement Layer 0 (index-free file/git facts) and optional Layer 1 (Tree-sitter AST extraction) repository intelligence. Build role-tailored `ContextManifest` objects. Enforce grammar licensing verification.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Layer 0 Index-Free Queries**: Implement baseline queries (`definitions`, `references`, `importers`) using `rg`, `git`, and Python `ast`.
- [ ] **Layer 1 Tree-Sitter Integration**:
  - Integrate Tree-sitter parser. Enforce parse-error tolerance for intermediate code.
  - Overlay worktree edits on top of base snapshot index.
- [ ] **Grammar Licensing Gate (`[INV-A09]`)**:
  - Add individual verification entries for the parser and EACH enabled grammar in `maestro/thirdparty.json`. Verify `maestro doctor --thirdparty` exits 0.
- [ ] **ContextManifest Assembly**:
  - Implement `maestro/repository/context.py` building bounded manifests tailored to role contracts.
  - Mandatory instructions and criteria are NEVER truncated.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Stale queries are rejected or flagged partial.
  - Worktree overlays cannot cross-contaminate.
  - Malformed syntax falls back safely to search/read.
  - Search and read fallbacks remain fully functional if Tree-sitter is absent.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/repository
  ```\n