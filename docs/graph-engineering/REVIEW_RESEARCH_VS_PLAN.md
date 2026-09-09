# Review: targeted research report vs. the graph-engineering plan

> **SUPERSEDED — 2026-09-08.** Every finding this document recommended adopting is now folded into
> [`MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html`](MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html),
> which is the single source of truth for the plan. Keep this file as the working record of *how*
> the research report and the local audit were reconciled; do not treat it as plan content, and do
> not amend the plan from it again.
>
> Two things in it are known stale: §2's `init` contract row (the exit-code defect does not
> reproduce on the current tree) and §3.3's "Antigravity quota unavailable" (its status-line payload
> carries four independent pools; the inspection had read `--help`). Both corrections are in the
> plan's amendment log.


**Reviewer:** overnight worker `c-maestro-1`, 2026-09-08.
**Sources reconciled:**

| Source | What it is | Produced |
|---|---|---|
| `docs/graph-engineering/blind-spots-report.md` | External targeted research pass with web citations (paid routing/quota, durability engines, repository intelligence) | 2026-09-08 02:49 |
| `artifacts/graph-engineering/*.json` + `tests/graph_engineering/` | Local empirical audit run against the installed CLIs and this machine, by the interactive Codex session | 2026-09-08 01:31 – 03:07 |
| `docs/graph-engineering/MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html` | The single authoritative plan (D01–D08, P0–P12, E0–E6) | 2026-09-07 22:23 |
| `GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md` | The open-evidence register (B01–B18) | 2026-09-07 |

**Standing rule applied throughout:** where the external report and the local measurement disagree, **the local measured result wins**, and the disagreement is recorded rather than smoothed over. Section 3 does that explicitly.

---

## 0. Evidence quality of the research report itself — read this first

Three properties of the report constrain how much of it may be baked into the plan.

1. **The report in this repository is a wrapper, not the deliverable.** Its first line links the actual research to
   `sandbox:/mnt/data/GRAPH_ENGINEERING_TARGETED_RESEARCH.md`. That file **does not exist on this machine** (`ls` → no such file) and is referenced nowhere else in the repo. The full telemetry schema, the complete measurement procedures and the source table it repeatedly defers to are therefore **not in our possession**. Everything below is a review of the 216-line summary only.
2. **The citations are not resolvable from here.** They are opaque `citeturn…` / `fileciteturn…` tokens, not URLs. No claim in the report can be independently re-checked from this repository, and none of them carry a retrievable access date beyond the report's own "verified on September 8, 2026" sentence. Treat every external claim as *single-sourced and unverifiable in-repo* until someone re-runs it.
3. **Dated commercial facts must not become constants.** The report says so itself, and it is right: plan names, prices ($20 / $20 / $19.99), model tier names (Sonnet 4.6; Sol/Terra/Luna; Gemini Pro/Flash), quota-window shapes and CLI flag surfaces are **runtime/catalog facts with a verification timestamp**, never literals in Maestro source or in the architecture HTML. This is B17 and B06 restated, and the local audit (§2) shows exactly why: the installed CLIs already disagree with the report's description of them.

**Consequence for the plan:** the report is usable for *direction* (which provider to shortlist, which metric to define, which invariant to add). It is not usable as *ground truth about installed software*. Every finding below is graded on that basis.

---

## 1. Finding-by-finding review of the research report

Grades: **Confirms** · **Sharpens** · **Contradicts** · **Orthogonal**.
Load-bearing: how much of the plan moves if the finding holds. Evidence: how well supported.

### R1 — Claude Code exposes structured `five_hour` / `seven_day` rate-limit percentages and reset timestamps; measurement quality "High"

- **Bears on:** B02, D05, P6, E1.
- **Grade vs. plan: sharpens.** **Grade vs. local measurement: contradicted — see §3.1.**
- **If it holds:** P6 could attribute subscription cost per task on Claude directly, and E1's routing calibration would need no proxy for that backend.
- **Load-bearing: high.** It is the report's single strongest claim and the foundation of its whole routing section.
- **Evidence: vendor documentation, single source, unverifiable in-repo — and locally falsified in part.** Do not write "High" into the plan.

### R2 — Codex has no stable `codex status --json`; needs a PTY `/status` collector; its quota displays are known to be stale or self-inconsistent

- **Bears on:** B02, B06, P6.
- **Grade: confirms the *risk*, contradicted on the *acquisition difficulty* — see §3.2.**
- **If it holds:** P6 needs a version-qualified parser plus a `censored`/`contaminated` observation state.
- **Load-bearing: medium-high.** The `contaminated` state it motivates is load-bearing; the "hard to acquire" premise is not.
- **Evidence: mixed — an open feature request plus community issue reports, explicitly labelled by the report as observation not policy.** That labelling is honest and should be preserved.

### R3 — Antigravity `/usage` refreshes quota from Google's backend and is a usable observable signal ("Medium")

- **Bears on:** B02, B06, P6.
- **Grade: contradicted for the installed version — see §3.3.**
- **If it holds:** Antigravity becomes routable under a quota budget.
- **Load-bearing: medium.** Without it, Antigravity can be *used* but not *budgeted*.
- **Evidence: vendor docs for some version; the version installed here does not have it.**

### R4 — Antigravity has *multiple independent quota pools* (Gemini pool normalised by pricing ratio vs. a separate fixed non-Gemini limit), so `account_pool_id` ≠ "Google account"

- **Bears on:** B02, B13, P6, P8.
- **Grade: sharpens.** The plan's account model is one pool per account; this says pool identity is `(account, model-family)`.
- **If it holds:** the `account_pool_id` key in P6/P8 admission must become a composite, and reserve computation must be per-pool. This is a **schema** change, cheap now and expensive later.
- **Load-bearing: high for schema, low for behaviour today.**
- **Evidence: vendor docs, single source, not locally testable (no usage surface installed).** Adopt the *schema generality*, not the specific Gemini ratio numbers.

### R5 — Four allowance-efficiency statistics (5h pp/verified task, weekly pp/verified task, verified tasks per 100% of each window), failures and rework counted in the numerator; **never sum the two windows**

- **Bears on:** B02, B18, D05, P6, E1.
- **Grade: sharpens — the most valuable single contribution of the report.**
- **If it holds:** P6/E1 acceptance criteria gain concrete, falsifiable metrics instead of "measure routing efficiency".
- **Load-bearing: high, and cheap.** It is a definition, not an adoption.
- **Evidence: derived reasoning, not an external fact. It needs no citation and carries no vendor risk.** Adopt as written, including the "no weighted sum" prohibition and the "route consumed allowance with zero verified successes ⇒ record 0, not blank" rule.

### R6 — Do not replace native durability now. Temporal / DBOS / Restate all leave Maestro owning process identity, worktree fencing, effect reconciliation and checkpointing

- **Bears on:** B09, D02, P1, P3, E6.
- **Grade: confirms — and §3.4 confirms it a second time from local measurement.**
- **If it holds:** D02 and E6 stand unchanged. This is the report agreeing with the plan.
- **Load-bearing: high, but the plan already has it right.** The valuable increment is the *ordering*: DBOS first (lowest architectural distance from a local Python app), Restate second (durable waits), Temporal only if a genuine multi-host requirement appears.
- **Evidence: vendor documentation of each engine's own semantics — at-least-once steps, documented "zombie" executors, activity heartbeat/idempotency requirements.** These are the engines' self-described limitations, which is the strongest kind of vendor citation: it is an admission against interest. Reasonable to adopt.
- **Also adopt:** the bake-off trigger is *demonstrated recovery defects or complexity*, not "Maestro contains retries."

### R7 — Repository intelligence: Tree-sitter as the smallest optional syntax layer; SCIP as the first deeper benchmark; Joern on-demand only; **drop Stack Graphs (archived Sept 2025)**

- **Bears on:** B10, B17, D07, P9, E2.
- **Grade: sharpens.** The plan holds an undifferentiated shortlist; this ranks it with reasons.
- **If it holds:** D07/P9 name Tree-sitter as the MVP provider and SCIP as the first benchmark rival, and the Stack Graphs row leaves the active shortlist.
- **Load-bearing: medium.** P9 is late and optional; getting the shortlist wrong costs benchmark time, not architecture.
- **Evidence: project self-description + one archived-repository state.** The Stack Graphs archival is a **dated repository-state fact** — record it with its verification date, and as a *shortlist* removal, not a technical refutation. If someone un-archives it the reasoning changes.
- **Explicitly do not adopt:** `tree-sitter-graph` as a requirement. The report is right that it is a general DSL that leaves language semantics as our problem.

### R8 — Structural retrieval does **not** replace exploration at equal quality (cited 2026 evaluation: ~10× fewer tokens and 2.1× fewer tool calls, but 83% vs 92% answer quality)

- **Bears on:** B10, D07, P9, E2.
- **Grade: confirms the plan's conservatism, sharpens the acceptance criterion.**
- **If it holds:** P9 must keep search/read fallback permanently, and E2 must measure *quality* alongside token savings — a provider that saves tokens and loses 9 points of quality fails.
- **Load-bearing: high for D07.** It is the difference between "index replaces exploration" and "index augments exploration".
- **Evidence: one cited external evaluation whose numbers we cannot check.** Adopt the **direction** (keep fallback; measure quality not just recall) and **do not quote the 83/92 figures in the plan.**

### R9 — `exploration_displacement = 1 − exploration_with_provider / exploration_baseline`, computed separately over source-read calls, bytes/tokens, and paid allowance

- **Bears on:** B10, P9, E2.
- **Grade: sharpens.** A concrete E2 metric where the plan has a qualitative goal.
- **Load-bearing: medium, cheap.** Definition only.
- **Evidence: derived, no vendor dependency.** Adopt.

### R10 — Credential boundary: launch the official CLI under its supported login; never extract OAuth/session credentials and reuse them as a generic API transport

- **Bears on:** B06, B17, D04, P6.
- **Grade: sharpens into an invariant.** The plan does this in practice; the report makes it a rule.
- **If it holds:** an explicit invariant in D04/P6 that forbids credential repurposing.
- **Load-bearing: high — this is a terms-of-service and account-safety boundary, not an optimisation.**
- **Evidence: two vendor policy statements.** Even if the exact wording drifts, the invariant is the safe side of the trade in every version. **Adopt unconditionally.** Locally consistent: all three backends are already driven as installed CLIs under `/home/dan/.local/bin/`.

### R11 — Bootstrap routing table (task class → route → effort), explicitly a prior to be superseded by Maestro's own outcomes

- **Bears on:** B02, B04, B12, P6.
- **Grade: orthogonal-to-mildly-useful, and partly undercut by §3.5.**
- **Load-bearing: low.** The plan already has "bootstrap routing without invented precision"; this fills it with vendor-tier marketing positioning (Sol "hardest work", Luna "high volume").
- **Evidence: vendor tier descriptions — the weakest category in the report.** Do **not** copy the table into the plan. Keep the plan's existing rule that priors are replaced by measured outcomes, and let P6/E1 populate the table from `artifacts/`.

### R12 — Re-validate model/catalog facts on **every relevant backend upgrade**, not on a calendar

- **Bears on:** B06, B17, D04, P6, P11.
- **Grade: sharpens.**
- **Load-bearing: medium-high.** It converts a documentation chore into a version-triggered gate.
- **Evidence: derived from observed churn (Codex surfaces changed during the research window).** Adopt; it costs nothing and §2 shows the installed CLIs already drifted from the docs.

### Orthogonal — the report is silent on these

B01 (representative outcomes), B03/B04 (free-model runtime and value of extra inference — explicitly out of the report's scope), B05 (init policy synthesis), B07 (filesystem enforcement), B08 (process recovery matrix), B11 (validation dependency completeness), B13 (concurrency/resource calibration), B15 (retention/disk), B16 (self-update and service ownership), B18 (evaluation breadth). **All ten remain open on the research side.** Six of them were moved by the local audit instead — §2.

---

## 2. What the local audit actually measured

Source: `artifacts/graph-engineering/local-audit.json` (`backend_calls_started: 0`, scope `local-machine-only`), `backend-review-results.json`, `init-rehearsal.json`. Machine-qualified, this host, 2026-09-07 23:40 – 2026-09-08 00:07 UTC.

| Blind spot | Measured result | Reading |
|---|---|---|
| **B02** | Claude 5h 10%→13%, weekly `null`→49%, `resets_at` null, `updated_at` empty, post-run sample names **Opus 5** though the review ran **Sonnet 4.6**. Codex 5h **84%→89% with zero Codex tasks launched**; weekly 82%→82%. Antigravity: no window signal at all. Attribution recorded as `unknown`. | Account-level windows are **contaminated by concurrent human use** and **lag**. Per-task attribution from these samples is not currently possible on any backend. |
| **B06** | `claude 2.1.263`: `--effort`, `--model`, `--restricted`, `--resume`; native resume ✓, sandbox ✗, system-prompt-file ✓. `codex-cli 0.153.4`: `--config`, `--model`, `--sandbox`, `resume`; **no `--effort` flag** — effort is a `--config` key (`gpt-5.6-sol`, high). `agy 1.1.27`: `--effort`, `--model`, `--sandbox`, `--conversation`; **not driven by Maestro**, no usage surface. | Effort control is **not uniform across vendors**. B06's "never invent flags, never silently map effort labels" is now measured, not asserted. |
| **B05** | 8 init-policy fixtures: **3 missed, 0 false** — `mixed-monorepo/subproject-test`, `generated-assets`, `service-dependency`. Single-language root cases (node/python/go, with and without tests) all correct. | The detector is *sound but incomplete*: it never invents a requirement, it misses structural ones. That asymmetry is the right one to have, and it bounds the fix. |
| **B07** | `logical_confinement: accepted, reason ok` — for an allowed scratch-relative path. Recorded claim: *"logical path checks do not establish OS-enforced read/write isolation."* | Honest negative. B07 is **not** closed. Path checking ≠ enforcement. |
| **B08** | `detached_process`: **child alive after launcher exit = true** (cleaned up afterwards, launcher exit 0). `exclusive_writer`: concurrent replacement **denied**, post-exit replacement **granted**. | The lock is correct *for live processes* — but a launcher can exit while its child still runs, and the lock is then released to a replacement. **This is the zombie-writer window, measured on this machine.** |
| **B11** | Dependency fixture: 6 cases, full acceptance suite exit 0, **`signature_only_selected: 0`**. | Signature-equality selection would have chosen **zero** of the six real dependencies (dynamic import, data file, generated code, env, schema, plugin). B11's premise is confirmed empirically. |
| **B13** | 4 tasks: serial 692 ms / max queue delay 503 ms / peak RSS 11.6 MiB; parallel@2 363 ms / 167 ms / 23.1 MiB. SQLite: 4 workers, 12 jobs, 16 writer attempts, **0 busy errors, 0 duplicate claims**, max writer wait 107 ms. | First calibration point. At this scale SQLite contention is a non-issue; RSS scales roughly linearly with concurrency. Nothing here justifies a richer scheduler yet. |
| **B14** | `blind_retry` → **effect_count 2**, not reconciled. `receipt_first` → 1, not reconciled. `receipt_retry` → **1, reconciled**. | A blind replay **duplicated the external effect**. Receipt-first + idempotent retry did not. Direct, reproducible justification for the receipt protocol. |
| **B15** | SQLite online backup/restore: `quick_check ok`, rows restored; missing artifact detected by ID. | Backup/restore path works at fixture scale. Disk-full and long-run volume still untested. |
| **B16** | Adopted-version pointer is `/home/dan/.maestro/current`, scope **machine-global**; generated service now `Type=forking` + direct named TMUX session. | Confirms B16's shared-pointer hazard as a *design consequence*, and confirms the template now matches Dan's `Type=forking`/TMUX rule. |
| **B12 / B18** | Seeded-defect review on one fixture: Claude Sonnet 4.6 @ low effort **17/17**; Antigravity gemini-3.8-flash-low **17/17 plus one extra true finding**. Codex not run (5h already at 84%). Claude reported `list_cost_usd 0.085`, `subscription_charge: unknown`. | **Ceiling effect.** Two different backends at *low* effort both saturated the fixture. This measures the fixture, not the models. The recorded limits say exactly this. |
| **init contract** | Two scratch projects (python, node): 19 files scaffolded, test command correctly detected, test adapter exit 0 — but **`init` exit code 1** because the no-op loop check could not create the `agents` tmux session. `halt_respected: false`, `queue_exhausted: false`. | `init` reports failure after a materially successful scaffold. A real P0 baseline finding: the init contract conflates "scaffolding failed" with "loop verification failed". |

---

## 3. Reconciliation — where the two sources disagree

**Rule: the local measurement wins. Reason: it is a direct observation of the software actually installed on the machine Maestro runs on, at a known time, with a retained raw artifact. The report describes vendor documentation for unspecified versions, via citations that cannot be resolved from this repository (§0).**

### 3.1 Claude quota measurement quality — report says "High", local says partial

The report calls Claude's structured rate-limit feed "the highest-confidence quota measurement" and says it gives Maestro "almost exactly the signal it needs."

Measured on `claude 2.1.263`: `five_hour.used_pct` was present (10, then 13). **`resets_at` was `null`. `seven_day` was `null` in the first sample.** `updated_at` was empty. And the post-run sample reported model **`Opus 5`** when the run under measurement used **Sonnet 4.6** — i.e. the sample reflects *whatever the account did most recently*, not the invocation being measured.

**Resolution:** Claude is the **best of the three**, not "high confidence". Grade it **medium**, with two named defects to design around: absent reset epochs (so epoch-change detection must tolerate `null`) and account-level, last-writer-wins sampling (so the sample is not attributable to an invocation without isolation). The report's own `censored`/`contaminated` mechanism is the right answer — it just needs to fire far more often than the report implies.

### 3.2 Codex acquisition difficulty — report says hard, local says already working

The report devotes a paragraph to needing a PTY `/status` collector because `codex status --json` is only a feature request.

Measured: Maestro's **existing usage telemetry** already returns Codex's windows as clean structured data — `windows["300"] = {used_pct 84, resets_at 1788825729}`, `windows["10080"] = {used_pct 82, resets_at 1789116737}` — with **real reset epochs**, which is more than Claude gave.

**Resolution:** Codex is currently the **best-instrumented** backend here, not the medium one. Do not build a PTY collector for Codex. **But** the report's warning is simultaneously confirmed in the strongest possible way: Codex's 5-hour figure moved **84% → 89% with zero Codex tasks launched by the audit**. So the contamination the report predicted is real and was observed on the first paired sample. Both halves matter: acquisition is solved, *attribution is not*.

### 3.3 Antigravity `/usage` — report says available, local says absent

The report treats Antigravity's `/usage` as a documented, backend-refreshed signal and designs a PTY collector plus a units→percentage calibration around it.

Measured on `agy 1.1.27`: `"usage": null`, `"usage_note": "no usage surface observed in installed CLI help or Maestro"`, and `maestro_driver: false` — Antigravity is not currently driven by Maestro at all.

**Resolution:** Antigravity quota measurement is **unknown**, not medium. The report's per-pool schema (R4) is still worth adopting because it is a schema generality that costs nothing; its collector design is **deferred until an installed version actually exposes the surface**, and P6 must be able to route with a `quota: unavailable` backend rather than assuming a reading exists.

### 3.4 Durability — the two sources agree, and the local result is the stronger argument

The report argues from Temporal/DBOS/Restate documentation that no engine relieves Maestro of process fencing and effect reconciliation. The local audit demonstrates the same conclusion **from Maestro's own behaviour**: a launcher exited while its child was still alive (B08), and a blind retry duplicated an external effect while a receipt-guarded retry did not (B14).

**Resolution:** D02 stands, E6 stays deferred, and the *justification recorded in the plan should be the local measurement*, with the engine documentation as corroboration. A measured duplicate effect is better evidence than three vendors' descriptions of their own semantics. Adopt the report's **ordering** (DBOS → Restate → Temporal) since local evidence says nothing about ordering.

### 3.5 Routing priors — report offers a table, local says the fixture cannot discriminate

The report's bootstrap table assigns high effort and a strong independent route to security-oriented review. The local seeded-defect review — which is exactly that task class — was **saturated at 17/17 by two different backends at *low* effort**, one of them a Flash-tier model.

**Resolution:** this does not show low effort suffices; it shows the **fixture has no discriminating power**. Record it as a measurement of the fixture. The actionable consequence is for **E1/E2 fixture design** — seeded-defect suites must be calibrated until a cheap route measurably fails them — not for the routing table. Reinforces the decision in R11 not to copy the vendor table into the plan.

### 3.6 Cost accounting — both sources agree, local makes it concrete

Report: never equate tokens or dollars with subscription consumption. Local: Claude reported `reported_list_cost_usd: 0.0852174` alongside `subscription_charge: "unknown"`, and Antigravity reported 43,683 tokens with `five_hour: unknown` / `weekly: unknown`.

**Resolution:** B02 confirmed by both. The plan must keep **token/context accounting and subscription-allowance accounting as separate, non-convertible quantities**, and a CLI's USD estimate is a list-price artefact, not evidence of a subscription charge.

---

## 4. What changes in the plan

Adopted into `MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html` in this pass:

| # | Change | Target | Basis |
|---|---|---|---|
| C1 | `QuotaObservation` becomes a first-class contract: 5-hour and weekly held **independently**, reset epoch **nullable**, used/remaining percentage *or* raw units, raw-artifact reference, source, `contaminated`/`censored`/`unavailable` states, confidence, provenance. | P6 | R1+R2, §3.1–3.3 |
| C2 | Per-backend measurement grades set from **local** observation: Codex *structured, reset epochs present*; Claude *partial, reset epoch may be null, account-level*; Antigravity *unavailable on the installed version*. Recorded as version-qualified facts with dates. | P6 | §3.1–3.3 |
| C3 | `account_pool_id` becomes composite `(account, model-family/pool)`; reserves computed per pool. | P6, P8 | R4 |
| C4 | Four allowance-efficiency statistics defined; failures/rework counted; **windows never summed**; zero-verified routes record `0`, not blank. | P6, E1 | R5 |
| C5 | Credential-boundary invariant: invoke the official installed CLI under its own login; never repurpose its credentials as an API transport. | D04, P6 | R10 |
| C6 | D02/E6 unchanged; justification restated on the **local** duplicate-effect and detached-child measurements; bake-off order DBOS → Restate → Temporal; trigger is demonstrated recovery complexity, not the presence of retries. | D02, P1/P3, E6 | R6, §3.4 |
| C7 | D07/P9: Tree-sitter named as the smallest optional syntax provider, SCIP as first deeper benchmark, Joern on-demand for data-flow/security only, Stack Graphs off the active shortlist (archived — dated fact), `tree-sitter-graph` explicitly not required. Search/read fallback is permanent. | D07, P9 | R7, R8 |
| C8 | `exploration_displacement` added to E2, computed separately over read calls, bytes/tokens and allowance; E2 acceptance requires **no quality regression**, not only token savings. | P9, E2 | R8, R9 |
| C9 | Catalog re-validation triggered by **backend version change**, not by calendar. | D04, P6, P11 | R12, §2 |
| C10 | Seeded-defect fixtures must be calibrated to discriminate — a fixture a cheap route saturates is not a quality gate. | E1, E2, P12 | §3.5 |

**Deliberately not adopted:** the vendor bootstrap routing table (R11); the 83%/92% quality figures (R8); prices and plan names as constants (§0.3); a PTY collector for Codex (§3.2); an Antigravity `/usage` collector while the installed version has no such surface (§3.3).

**Still open after both passes:** B01, B03, B04, B07 (path checks are not enforcement), B15 (disk-full, long-run volume), B18. B05, B08, B11, B13, B14, B16 have first local evidence but are not closed.
