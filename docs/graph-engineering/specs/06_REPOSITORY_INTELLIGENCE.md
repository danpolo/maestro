# MAESTRO REPOSITORY INTELLIGENCE & CONTEXT MANIFESTS (AGENT SPECIFICATION)

```
Document ID:     MGES-REPO-CONTEXT-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Code Navigation, AST Extraction, Context Assembly, Query Interfaces
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Repository & Context Specification
```

---

## 1. THE MULTI-LAYER REPOSITORY INTELLIGENCE MODEL

Maestro avoids bloated universal code graphs or heavyweight semantic indexes by establishing a tiered, progressive intelligence architecture:

```
[Layer 0: Index-Free Facts] ──► Git tree, rg, project manifests, Python AST
             │
             ▼
[Layer 1: Tree-Sitter AST]  ──► Parse-error tolerant syntax tree, symbol definitions
             │
             ▼ (Gated by Experiment E2)
[Layer 2: SCIP Navigation]  ──► Precise definition/reference cross-file navigation
             │
             ▼ (On-Demand Gated)
[Layer 3: Joern Data-Flow]  ──► Code property graphs for security/taint analysis only
```

---

## 2. PROVIDER EVALUATION & SELECTION RULES

### 2.1 Provider Order & Technical Rationale (D07)
1. **Layer 0 (Index-Free Baseline)**:
   - Always available, zero setup.
   - Bounded `rg`, `git diff`, Python standard library `ast`, package configuration files.
2. **Layer 1 (Tree-sitter)**:
   - First optional structural provider.
   - **Critical Property**: Exceptional tolerance for syntactically broken or incomplete code states that agent worktrees naturally produce during intermediate edits.
   - Extractor emits parse-error and completeness metadata with every result.
3. **Layer 2 (SCIP - Sourcegraph Code Intelligence Protocol)**:
   - Primary benchmark rival for precise cross-file reference resolution.
   - Requires isolated worktree overlay handling.
4. **Layer 3 (Joern Code Property Graph)**:
   - Invoked exclusively on-demand for specific complex refactoring, data-flow analysis, or security vulnerability tasks.
   - NEVER used as an always-on navigation layer.

### 2.2 Strict Negative Invariants on Repository Intelligence
- `[STRICT_NEGATIVE_CONSTRAINT] NO_TREE_SITTER_GRAPH`: The `tree-sitter-graph` DSL is explicitly rejected; language semantics are our responsibility, and it adds external DSL overhead without semantic benefit.
- `[STRICT_NEGATIVE_CONSTRAINT] NO_SYMDEX`: SymDex indexing MUST NEVER be executed directly on the host server outside an isolated, contained container/worktree.
- `[STRICT_NEGATIVE_CONSTRAINT] NO_LLM_KNOWLEDGE_GRAPHS`: Do NOT extract vector embeddings, neo4j graphs, or LLM-summarized knowledge bases for source code.
- `[STRICT_NEGATIVE_CONSTRAINT] SEARCH_READ_FALLBACK_PERMANENT`: Textual search (`rg`, `find`) and file reading MUST remain permanently supported. Structural providers are purely optional accelerators.

### 2.3 Licensing & Third-Party Gates (`[INV-A09]`)
- Each parser and **each individual language grammar** must have its own verification entry in `maestro/thirdparty.json`.
- Grammars ship independently from parsers and carry distinct open-source licenses.
- `maestro doctor --thirdparty` MUST exit 0 before a grammar is loaded.

---

## 3. THE REPOSITORY QUERY API (`RepositoryQueries`)

Managed by `maestro/repository/queries.py` and backed by the disposable `.orchestrator/repository.sqlite3` index:

```yaml
RepositoryQueries:
  definitions(name: str, snapshot: str) -> list[SymbolDefinition]
  references(symbol: str, snapshot: str, direction: str, depth: int) -> list[Reference]
  importers(file: str, snapshot: str) -> list[ImportRelation]
  affected(changed_inputs: list[str], snapshot: str) -> AffectedAnalysisResult
  tests_for(subjects: list[str], snapshot: str) -> list[TestLinkage]
  context(task: TaskSpec, role: AgentDefinition, snapshot: str, policy: Policy) -> ContextManifest
```

---

## 4. CONTEXT MANIFEST GENERATION (`ContextManifest`)

Agents MUST NOT be fed raw uncurated directories. The context builder (`maestro/repository/context.py`) compiles a role-tailored, budget-bounded `ContextManifest`:

```yaml
ContextManifest:
  snapshot_id: str                  # Pinned Git commit SHA
  task_id: str                      # Target task ID
  role_definition_hash: str         # Applied role contract hash
  policy_hash: str                  # Applied project policy hash
  mandatory_instructions: list[str] # Top-priority system constraints (NEVER truncated)
  acceptance_criteria: list[str]    # Verifiable task goals (NEVER truncated)
  entrypoints: list[str]            # Primary files targeted for edit
  source_refs: list[dict]           # Exact file paths with line ranges and content hashes
  related_tests_and_contracts: list[dict] # Pinned test files and schema references
  relevant_decisions: list[dict]    # Historical architecture decisions applicable to this task
  prior_failure_refs: list[str]     # Diagnostic evidence from earlier failed attempts (if any)
  retrieval_paths: list[str]        # Explored search paths
  freshness: str                    # 'exact_match' | 'partial' | 'stale'
  unresolved_queries: list[str]     # Queries that failed or produced partial results
  omitted_material: list[str]       # Content pruned due to context budget limits
  context_estimate_tokens: int      # Computed prompt token volume
```

### 4.1 Role-Specific Context Bounding Rules
- **Implementer**: Receives mandatory instructions, acceptance criteria, target file slices, local test fixtures, and prior attempt diagnosis. Broad architectural history is omitted.
- **Planner**: Receives system architecture decisions, component dependency maps, public interface signatures, and failure classes. Full source bodies are omitted.
- **Reviewer**: Receives immutable diff between base and candidate, acceptance criteria, and controller test results. Implementer conversational narrative is withheld to avoid confirmation bias.
- **Test Designer**: Receives acceptance criteria and public interfaces. Implementer candidate patch is withheld to prevent copying incorrect implementation assumptions.
