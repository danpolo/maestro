# MIGRATION GUARDRAILS (CROSS-CUTTING INVARIANTS FOR EVERY PHASE)

```
Document ID:     MGES-GUARDRAILS-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Scope:           Mandatory rules governing all phase implementations P00 through P12
```

---

## 1. THE THIRD-PARTY VERIFICATION GATE (`maestro doctor --thirdparty`)
- Any new or materially updated backend CLI, or any optional external dependency/grammar, MUST pass implementation-time capability, version, licensing, and conformance verification before being marked supported.
- Verification is recorded against the exact installed version in `maestro/thirdparty.json`.
- `maestro doctor --thirdparty` is the gate: it MUST exit 0. It exits non-zero on an unverified or drifted subject that is in use.

---

## 2. SHADOW MODE & MUTUAL EXCLUSION
- Shadow mode observes; it NEVER executes a second copy of an external side effect.
- The legacy runner and graph runner are **mutually exclusive** per project run.
- Compatibility facades are retained only while callers are being migrated; duplicate state writers and duplicate roadmap parsers MUST be deleted at their cutover.
- Existing project data and uncommitted work MUST be preserved.

---

## 3. STRICT BOUNDARIES
- No optimization may lower established quality gates to meet a performance or token target.
- The critical path is strictly: `P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P11 -> P12`.
- P8–P10 are developed only after their prerequisite inputs exist and remain OFF by default until qualified under Experiments E1–E5.\n