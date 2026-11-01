"""How the graph runner routes an agent node: manifest -> binding -> invocation (P12A W1/W2).

`WorkflowRunner` owns *when* a node is claimed and launched; this module owns *what* the
routing components are and how they fit together, so the runner never imports the
catalog, the router or the repository layer inline. `DispatchRouting` is the one object
the graph loop builds (once, in `orchestrator._graph_routing`) and hands to every runner;
`routing=None` on a runner means "no routing", which is the pre-P12A behaviour exactly.

Three rules carried over from the phase spec, each enforced by the shape here:

* **The manifest is the routing input** (W2). `resolve` hands the `ContextManifest`
  itself to `router.resolve_agent`; the router does `RoutingContext.from_manifest`, and
  nothing in this module builds a `RoutingContext` by hand.
* **A stale snapshot never reaches the router.** `build_manifest` lets
  `StaleSnapshotError` / `UnknownSnapshotError` / `ContextError` propagate, and the
  runner turns `MANIFEST_REFUSALS` into a failed node before `resolve` is ever called.
* **One binding, four consumers.** The argv (`render_binding_argv`, from
  `router.dispatch_plan`) and the claim deadline (`deadline_sec`, from
  `router.execution_deadline_sec`) both read the binding the runner resolved once.

`router` is referenced through its module on purpose, so a test can spy on
`maestro.backends.router.resolve_agent` and count the calls a poll makes.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from typing import Any, Optional

from maestro.backends import router
from maestro.backends.catalog import (CapabilityCatalog, OutcomeStatistics, ResourcePoolState,
                                      RESET_JITTER_SEC)
from maestro.repository.context import ContextError, build_context_manifest
from maestro.repository.index import StaleSnapshotError, UnknownSnapshotError

__all__ = [
    "MANIFEST_REFUSALS",
    "DispatchRouting",
    "SessionDecision",
    "build_manifest",
    "resolve",
    "deadline_sec",
    "plan_for",
    "runtime_kind",
    "session_decision",
    "agent_version",
    "usage_to_dict",
    "read_usage",
    "read_attempt_tokens",
    "driver_attempt_tokens",
    "reset_crossing",
    "load_statistics",
    "record_outcome",
    "binding_view",
    "shape_key_from_json",
    "render_binding_argv",
    "endpoint_from_config",
]

#: What `build_context_manifest` raises when it must not route on the facts it has.
#: `UnknownSnapshotError` subclasses `StaleSnapshotError`; it is named for the reader.
MANIFEST_REFUSALS = (StaleSnapshotError, UnknownSnapshotError, ContextError)


def _no_usage(binding: Any) -> None:
    return None


def _no_planner_complexity(task_id: str) -> None:
    return None


def _no_sprint_refresh(resources, now_epoch: float) -> None:
    return None


def _fresh_pause_usage(driver_name: str) -> Any:
    """Read account quota directly; a statusline or rollout cache is not recovery proof."""
    from maestro.backends import registry

    driver = registry.get_backend(driver_name)
    reader = getattr(driver, "subscription_usage", None) or getattr(driver, "usage", None)
    return None if reader is None else reader()


def driver_usage(binding: Any) -> Any:
    """Bridge-first quota for the binding's pool, with a fresh native fallback.

    Production's `usage_reader`: the quota windows an attempt started and settled under
    (P12 §2). No handle is passed, so the sample names no session and is never mistaken
    for one conversation's context.
    """
    from maestro.backends import registry
    from maestro.usage_sources import get_usage_source, native_account_usage

    driver_name = registry.driver_name_for_backend_id(binding.backend_id)
    pool_id = getattr(binding, 'usage_pool_id', '')
    if not pool_id:
        # Historical callers without a pool retain their account-only native contract.
        return native_account_usage(driver_name)
    return get_usage_source().read(pool_id, driver_name=driver_name)


def _no_tokens(binding: Any, workspace: Any) -> None:
    return None


def driver_attempt_tokens(binding: Any, workspace: Any) -> Optional[dict]:
    """Production's `token_reader`: the finished attempt's cumulative token totals.

    Read at settle from the attempt's own inbox (`workspace`), where the call's session
    record is named (Claude's `session_uuid.txt`, Codex's `impl.log` header). `None` for a
    driver that cannot say, or when the record is gone (G6).
    """
    from maestro.backends import registry

    driver = registry.get_backend(registry.driver_name_for_backend_id(binding.backend_id))
    reader = getattr(driver, "attempt_token_usage", None)
    return None if reader is None else reader(workspace)


def observed_agent_version(binding: Any) -> Optional[str]:
    """`--version` of the CLI that serves `binding` as installed now, or `None`.

    Production's `version_reader`. The register row's version is the one last
    *verified*; a harness that auto-updates runs a newer one, and the attempt must
    record what actually ran.
    """
    import importlib

    from maestro.backends import registry

    name = registry.driver_name_for_backend_id(binding.backend_id)
    ref = registry.BACKENDS.get(name)
    binary = name
    if ref is not None:
        binary = getattr(importlib.import_module(ref.module), "BINARY", name)
    info = registry.resolve_binary(binary)
    return None if info is None else (info.version or None)


def _admitted(runner: Any, node: Any) -> None:
    return None


def _no_release(runner: Any, attempt_id: str) -> None:
    return None


def _no_block_hook(runner: Any, attempt_id: str, record: Any, pause: Any) -> None:
    return None


def extend_pause(pool: Any, *, until: int, reason: str) -> None:
    """Pause `pool` until `until`, never shortening a pause it already has (P12B F4)."""
    current = pool.paused_until
    if current is None or int(until) > int(current):
        pool.pause(until=int(until), reason=reason)


def _zulu_epoch(value: str) -> Optional[int]:
    from datetime import datetime, timezone

    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


@dataclass
class DispatchRouting:
    """Everything an agent dispatch consults, built once per controller process."""

    catalog: CapabilityCatalog
    policy: Any
    queries: Any
    task_spec: Callable[[str], Any]
    statistics: OutcomeStatistics = field(default_factory=OutcomeStatistics)
    resources: ResourcePoolState = field(default_factory=ResourcePoolState)
    trace: Any = None
    #: `binding -> Usage | None`. `None` means "not measurable here", never "no usage".
    usage_reader: Callable[[Any], Any] = _no_usage
    pause_usage_reader: Callable[[str], Any] = _fresh_pause_usage
    pause_probe_at: dict = field(default_factory=dict)
    #: `(binding, attempt inbox) -> dict | None`: the attempt's cumulative tokens, at settle.
    token_reader: Callable[[Any, Any], Any] = _no_tokens
    #: `binding -> str | None`: the CLI version as installed. `None` here (the default)
    #: means "read the catalog's version"; a reader that answers `None` means unobserved.
    version_reader: Optional[Callable[[Any], Optional[str]]] = None
    #: W8 host admission: `(runner, node) -> answer with .reason/.detail`, or `None` (admitted).
    admit: Callable[[Any, Any], Any] = _admitted
    #: W8 release when an attempt finishes.
    on_attempt_finished: Callable[[Any, str], None] = _no_release
    #: W5/W6: `shadow` (the full suite runs) or `authoritative` (only affected checks run).
    #: The graph loop sets `authoritative` only when the stored comparisons qualify.
    affected_mode: str = "shadow"
    #: W7: plan repairs a task may use; `None` is unbounded. The graph loop sets it from
    #: `engineering.max_plan_repairs`.
    max_plan_repairs: int | None = None
    #: P12B F4: `(runner, attempt_id, record, pause_row | None)` after a blocked requeue.
    #: The graph loop sets it to journal, mirror to the legacy quota table and notify.
    on_blocked: Callable[[Any, str, Any, Any], None] = _no_block_hook
    #: P12B F4: how long an `auth_failed` pauses its pool. Set from config by the graph loop.
    auth_failure_pause_sec: int = 3600
    #: P12B F5: the longest a node waits on a pool whose reset is unknown.
    max_blocked_wait_sec: int = 86400
    #: P12B F5: task ids whose `fallback: step_down` passed `compiler.fallback_refusal`.
    step_down_tasks: set = field(default_factory=set)
    #: P12B F5: paused pools whose reset is unknown (a backoff, not a stated reset).
    unknown_reset_pools: set = field(default_factory=set)
    #: `{role id: strength}` from config (DESIGN.md §17.2, `model_policy.role_strengths`).
    #: A role it does not name keeps its template's floor.
    role_strengths: dict = field(default_factory=dict)
    role_requirements: Mapping[str, Mapping] = field(default_factory=dict)
    role_independence: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {'reviewer': ('implementer',)})
    near_cost_margin: float = 0.20
    model_policy_revision: str | None = None
    pace_probe_at: dict = field(default_factory=dict)
    pace_estimates: Mapping = field(default_factory=dict)
    pace_history_reader: Callable[[], Sequence[Mapping]] = tuple
    runaway_mode: str = 'off'
    #: W13 may later provide a real planner forecast; reviewer hindsight is never a forecast.
    planner_complexity_reader: Callable[[str], int | None] = _no_planner_complexity
    project_priority: float = 1.0
    #: Production reads current effective config at each new budget transaction.
    #: Standalone injected routing may instead supply the scalar above.
    project_priority_reader: Callable[[], float] | None = None
    sprint_refresh: Callable[[ResourcePoolState, float], Any] = _no_sprint_refresh
    #: Read current controller state at resolution, including immediate controls.
    sprint_reader: Callable[[], Mapping] = dict

    def reserve_selected(self, runner, node, binding, now_epoch):
        """B7: bind the real selected cost after resolution, before the sole claim."""
        from maestro.control.models import attempt_id
        from maestro.control.resources import HostReservationLedger
        from maestro.workflows.pace import PaceEstimate
        if binding.pace_decision is None:
            return None
        candidate = next(c for c in binding.pace_decision.candidates
            if (c['backend_id'], c['model_id'], c['effort'] or '') ==
               (binding.backend_id, binding.model_id, binding.requested_effort or ''))
        estimate = PaceEstimate(**candidate['estimate'])
        observation = self.resources.pool(binding.usage_pool_id).observation
        amounts, capacity, resets = {}, {}, {}
        # Burn is measured in percent of the five-hour account window. Applying
        # it to weekly/model-scoped percentages would invent a unit conversion.
        # Their independent observed hard gates already ran during resolution.
        window = observation.five_hour
        amounts['account:300'] = estimate.charge_pct(binding.relative_cost, window.resets_at, now_epoch)
        capacity['account:300'] = max(self.resources.pause_pct - window.used_pct, 0.0)
        resets['account:300'] = window.resets_at
        priority = (self.project_priority_reader() if self.project_priority_reader is not None
                    else self.project_priority)
        return HostReservationLedger().reserve_budget(binding.usage_pool_id, amounts,
            capacity=capacity, resets=resets, priority=priority,
            project_id=str(runner.repo),
            run_id=attempt_id(runner.run_id, node.node_id, runner.attempt_index(node.node_id) + 1),
            writer_pid=os.getpid(), projection=dict(started_at=now_epoch,
                runtime_sec=estimate.runtime_sec, full_amounts={
                    'account:300': binding.relative_cost * estimate.burn_pct_per_cost}))

    def reconcile_budget(self, runner):
        from maestro.control.resources import HostReservationLedger
        live = {}
        for row in runner.store.conn.execute(
                "SELECT attempt_id, binding_json FROM attempts WHERE status IN ('claimed', 'running')"):
            document = json.loads(row['binding_json'] or '{}')
            live[row['attempt_id']] = document.get('pace_budget')
        HostReservationLedger().sync_budget(str(runner.repo), live, writer_pid=os.getpid())

    def attempt_finished(self, runner, attempt_id):
        from maestro.control.resources import HostReservationLedger
        # Ownership follows the admitted attempt, even after a policy change.
        HostReservationLedger().release_budget(str(runner.repo), attempt_id)
        self.on_attempt_finished(runner, attempt_id)

    def refresh_pace(self, now_epoch: float) -> None:
        """Refresh each distinct pool at most once per 300 s per driver/controller."""
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from maestro.backends.catalog import QuotaObservation

        pools_by_backend: dict[str, dict[str, Any]] = {}
        for entry, model in self.catalog.models():
            if entry.pairs(model):
                pools_by_backend.setdefault(entry.backend_id, {})[entry.pool_for(model)] = entry
        for backend, pools in pools_by_backend.items():
            if now_epoch - self.pace_probe_at.get(backend, float("-inf")) < 300:
                continue
            self.pace_probe_at[backend] = now_epoch
            written_at = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat()
            for pool_id, entry in pools.items():
                try:
                    usage = self.usage_reader(SimpleNamespace(backend_id=backend, usage_pool_id=pool_id))
                    if usage is None or getattr(usage, "error", None):
                        continue
                    split = getattr(usage, 'pools', None) or {}
                    if pool_id in split:
                        from dataclasses import replace
                        usage = replace(usage, windows=split[pool_id])
                    elif len(pools) > 1:
                        continue  # an unsplit account cannot invent another pool's quota
                    observed = QuotaObservation.from_usage(
                        usage, pool_id=pool_id, acquisition="pull", written_at=written_at,
                        staleness_bound=getattr(usage, 'staleness_bound', 900.0))
                    self.resources.pool(pool_id, concurrency_limit=entry.concurrency_limit).observation = observed
                except Exception:  # reader failure leaves this pool's previous observation to age out
                    continue
        self.sprint_refresh(self.resources, now_epoch)

    def note_pause(self, pool_id: str, *, reset_known: bool) -> None:
        """Remember whether `pool_id`'s current pause ends at a known reset (P12B F5)."""
        if reset_known:
            self.unknown_reset_pools.discard(pool_id)
        else:
            self.unknown_reset_pools.add(pool_id)

    def reconcile_pauses(self, store: Any, now_epoch: float) -> list[tuple[str, str, str]]:
        """Retire quota pauses only when newer account telemetry proves headroom."""
        import json
        from datetime import datetime, timezone

        from maestro import quota
        from maestro.backends import registry
        from maestro.backends.base import to_usage_json
        from maestro.backends.catalog import QuotaObservation

        now_iso = datetime.fromtimestamp(now_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        drivers: dict[str, str] = {}
        for entry, model_id in self.catalog.models():
            try:
                drivers[entry.pool_for(model_id)] = registry.driver_name_for_backend_id(
                    entry.backend_id)
            except registry.UnknownBackend:
                continue
        active = store.active_pool_pauses(now_iso)
        active_by_pool = {row["pool_id"]: row for row in active}
        # A crash can land between the control-store recovery marker and the legacy
        # state event. Complete that second write on restart before rehydrating pauses.
        mirror_resets = quota.exhausted_backend_resets()
        for marker in store.recovered_pool_pauses():
            driver = drivers.get(marker["pool_id"])
            newer = active_by_pool.get(marker["pool_id"])
            if (driver and mirror_resets.get(driver) == marker["paused_until"]
                    and (newer is None or newer["recorded_at"] <= marker["recovered_at"])):
                quota.clear_backend_exhausted(
                    driver, expected_reset_at=marker["paused_until"])
        recovered: list[tuple[str, str, str]] = []
        for row in active:
            if not row["reset_known"] or row["failure_kind"] not in (
                "quota_exhausted", "rate_limited",
            ):
                continue
            pool_id = row["pool_id"]
            driver = drivers.get(pool_id)
            if driver is None or now_epoch - self.pause_probe_at.get(driver, float("-inf")) < 300:
                continue
            self.pause_probe_at[driver] = now_epoch
            try:
                usage = self.pause_usage_reader(driver)
            except Exception:  # telemetry failure is an unknown, never headroom
                continue
            if usage is None or getattr(usage, "error", None) or getattr(usage, "source", None) not in (
                "codex_app_server", "claude_oauth_usage_api",
            ):
                continue
            sampled_at = _zulu_epoch(getattr(usage, "updated_at", ""))
            recorded_at = _zulu_epoch(row["recorded_at"])
            if (sampled_at is None or recorded_at is None or sampled_at < recorded_at
                    or abs(now_epoch - sampled_at) > 120):
                continue
            windows = getattr(usage, "windows", None) or {}
            if not windows or any(
                not isinstance(w.used_pct, (int, float)) or isinstance(w.used_pct, bool)
                or w.used_pct >= 100 for w in windows.values()
            ):
                continue
            attempt = store.conn.execute(
                "SELECT usage_start_json, usage_settle_json FROM attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            required: set[int] = set()
            if attempt is not None:
                for field in ("usage_start_json", "usage_settle_json"):
                    try:
                        prior = json.loads(attempt[field] or "null")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(prior, dict) and isinstance(prior.get("windows"), dict):
                        required.update(int(key) for key in prior["windows"] if str(key).isdigit())
            if not required.issubset(windows):
                continue
            source = usage.source
            store.record_pool_recovery(
                row, recovered_at=now_iso, usage_source=source,
                usage_json=json.dumps(to_usage_json(usage), sort_keys=True),
            )
            quota.clear_backend_exhausted(driver, expected_reset_at=row["paused_until"])
            pool = self.resources.pool(pool_id)
            pool.resume()
            pool.observation = QuotaObservation.from_usage(
                usage, pool_id=pool_id, acquisition="pull", written_at=now_iso,
            )
            recovered.append((pool_id, driver, source))
        return recovered

    def refresh_pauses(self, store: Any, now_epoch: float) -> None:
        """Rebuild the in-memory pool pauses from durable state (P12B F4).

        Called at the start of every poll, so a pause survives a controller restart: the
        `pool_pauses` table is the source, memory is only a cache. The legacy loop's
        `backend_exhausted` table is folded in too, onto every catalog pool its driver
        serves.
        """
        from datetime import datetime, timezone

        from maestro import quota
        from maestro.backends import registry

        limits: dict[str, int] = {}
        drivers: dict[str, set[str]] = {}
        for entry, model_id in self.catalog.models():
            pool_id = entry.pool_for(model_id)
            limits.setdefault(pool_id, entry.concurrency_limit)
            try:
                name = registry.driver_name_for_backend_id(entry.backend_id)
            except registry.UnknownBackend:
                continue
            drivers.setdefault(name, set()).add(pool_id)

        now_iso = datetime.fromtimestamp(now_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.unknown_reset_pools.clear()
        for row in store.active_pool_pauses(now_iso):
            until = _zulu_epoch(row["paused_until"])
            if until is None:
                continue
            pool = self.resources.pool(
                row["pool_id"], concurrency_limit=limits.get(row["pool_id"], 1)
            )
            extend_pause(pool, until=until, reason=row["reason"])
            self.note_pause(row["pool_id"], reset_known=bool(row["reset_known"]))
        for name, reset_at in quota.exhausted_backend_resets().items():
            until = _zulu_epoch(reset_at)
            if until is None:
                continue
            for pool_id in sorted(drivers.get(name, ())):
                pool = self.resources.pool(pool_id, concurrency_limit=limits.get(pool_id, 1))
                extend_pause(pool, until=until, reason=f"{name} quota_exhausted until {reset_at}")
                self.note_pause(pool_id, reset_known=True)


@dataclass(frozen=True)
class SessionDecision:
    """Fresh or resumed, and under which ids. Decided in exactly one place."""

    mode: str
    session_id: str
    resume_id: Optional[str] = None


def build_manifest(routing: DispatchRouting, runner: Any, node: Any):
    """The node's `ContextManifest`, pinned to the run's `code_base_sha` (W2).

    Raises one of `MANIFEST_REFUSALS` rather than routing on stale or unbounded context.
    A rework's writer and reviewer also get the files earlier blocking findings named
    (`WorkflowRunner.rework_paths`; Dan, 2026-09-28).
    """
    policy = routing.policy
    definitions = getattr(policy, "agent_definitions", None) or {}
    role = definitions.get(node.agent_definition_ref)
    if role is None:
        raise ContextError(
            f"{node.node_id}: policy defines no role {node.agent_definition_ref!r}; "
            "refusing to assemble a manifest for an undescribed role"
        )
    task = routing.task_spec(runner.revision.task_id)
    rework = getattr(runner, "rework_paths", None)
    return build_context_manifest(
        task, role, runner.revision.code_base_sha, policy, queries=routing.queries,
        rework_paths=rework(node) if callable(rework) else (),
    )


def resolve(
    routing: DispatchRouting, node: Any, manifest: Any, *,
    exclude: frozenset = frozenset(), now_epoch: Optional[float] = None,
    task_id: str = "", stepped_down_before: bool = False,
    soft_exclude: frozenset = frozenset(),
    strength_floor: Optional[str] = None, billing_only: bool = False,
    independent_families: frozenset[str] = frozenset(),
    history_store: Any = None,
):
    """`router.resolve_agent` over this routing's catalog, pools, policy and statistics.

    P12B F5: a `wait` is re-resolved once, one strength rung lower and with no hybrid
    billing grant, when `task_id` is in `step_down_tasks` and the node has not stepped
    down before; a bound result is marked `stepped_down`, anything else keeps the wait.
    A `pool_paused` wait also says whether any paused pool has a known reset.

    P12B F6: `soft_exclude` is the route the second retry would rather not reuse — the
    model that produced the bad output. It is a *preference*, not the hard `exclude`: with
    no other equal-or-better route the same model is bound again and the binding says so,
    because waiting for a route that may never appear is worse than one more try. Both
    passes are inside this one call, so a node still resolves exactly once per attempt.

    P12B F7: `strength_floor` raises the demand to at least that rung (the diagnoser is
    escalated above the model that failed), and `billing_only` pins billing to the
    objective's own permitted categories — so a free-tier project's diagnoser stays free
    even when a hybrid overlay would have granted it a paid route. Both are applied to
    every pass of this call, which keeps the floor from leaking away on a fallback.
    """
    import dataclasses

    base: dict = {}
    statistics = routing.statistics
    role_strength = routing.role_strengths.get(str(getattr(node, "agent_definition_ref", "") or ""))
    if role_strength:
        base['role_strength'] = role_strength
    base['role_requirement'] = routing.role_requirements.get(
        str(getattr(node, "agent_definition_ref", "") or ""))
    base['near_cost_margin'] = routing.near_cost_margin
    base['independent_families'] = independent_families
    base['pace_estimates'] = routing.pace_estimates
    if getattr(routing.policy, 'objective', 'paid_efficiency') == 'pace':
        base['pace_sprints'] = routing.sprint_reader().get('decisions', {})
        base['pace_history'] = (load_pace_history(history_store) if history_store is not None
                                else routing.pace_history_reader())
        if history_store is not None:
            # Aggregate outcomes can outlive detailed telemetry. Refresh this
            # resolution's fallback too, so a prior in-memory fold cannot hide
            # an authoritative amendment when no detailed attempt row remains.
            statistics = load_statistics(history_store,
                minimum_observations=routing.statistics.minimum_observations)
        base['pace_runaway_mode'] = routing.runaway_mode
        base['planner_complexity'] = (routing.planner_complexity_reader(task_id)
                                      if routing.runaway_mode != 'off' else None)
    if strength_floor:
        base["min_strength_floor"] = strength_floor
    if billing_only:
        objective = getattr(routing.policy, "objective", "paid_efficiency")
        base["billing"] = router.OBJECTIVE_BILLING.get(
            objective, router.OBJECTIVE_BILLING["paid_efficiency"]
        )

    def attempt(**overrides):
        excluded = overrides.pop("exclude", exclude)
        return router.resolve_agent(
            node, manifest, routing.resources, routing.catalog, routing.policy,
            statistics=statistics, trace=routing.trace,
            exclude=excluded, now_epoch=now_epoch, **{**base, **overrides},
        )

    if soft_exclude:
        preferred = attempt(exclude=exclude | soft_exclude)
        if preferred.is_bound:
            return preferred
        resolved = attempt()
        if resolved.is_bound:
            return dataclasses.replace(
                resolved,
                selection_reason=(
                    f"{resolved.selection_reason}; retry 2: no other equal-or-better "
                    f"route, same model reused"
                ),
            )
    else:
        resolved = attempt()
    if resolved.is_bound or resolved.disposition != "wait":
        return resolved
    if (getattr(routing.policy, 'objective', 'paid_efficiency') != 'pace'
            and task_id in routing.step_down_tasks and not stepped_down_before):
        objective = getattr(routing.policy, "objective", "paid_efficiency")
        billing = router.OBJECTIVE_BILLING.get(
            objective, router.OBJECTIVE_BILLING["paid_efficiency"]
        )
        stepped = attempt(strength_offset=-1, billing=billing)
        if stepped.is_bound:
            return dataclasses.replace(stepped, stepped_down=True)
    if resolved.reason == "pool_paused":
        resolved = dataclasses.replace(resolved, reset_known=_any_reset_known(routing, resolved))
    return resolved


def _any_reset_known(routing: DispatchRouting, unavailable: Any) -> bool:
    """True when some `pool_paused` rejection names a pool whose reset is known."""
    for route in unavailable.rejected_routes:
        if route.reason != "pool_paused":
            continue
        for entry, model_id in routing.catalog.models():
            if (entry.backend_id, model_id) == (route.backend_id, route.model_id):
                if entry.pool_for(model_id) not in routing.unknown_reset_pools:
                    return True
    return False


def deadline_sec(binding: Any, node: Any) -> int:
    return router.execution_deadline_sec(binding, node)


def plan_for(binding: Any):
    return router.dispatch_plan(binding)


def runtime_kind(routing: DispatchRouting, binding: Any) -> str:
    return routing.catalog.entry(binding.backend_id).runtime_kind


def session_decision(runner: Any, node: Any, claim: Any) -> SessionDecision:
    """The single fresh-vs-resume decision (P12A §4). Always fresh until a resume exists."""
    return SessionDecision(mode="fresh", session_id=claim.attempt_id, resume_id=None)


def agent_version(routing: DispatchRouting, binding: Any) -> Optional[str]:
    """The installed version of the CLI or runtime this binding names.

    Observed through `routing.version_reader` when one is wired (production); otherwise
    the catalog's version. Unobservable or unknown yields `None` rather than a guess.
    """
    if routing.version_reader is not None:
        try:
            return routing.version_reader(binding) or None
        except Exception:  # noqa: BLE001 — telemetry never fails the dispatch it observes
            return None
    try:
        return routing.catalog.entry(binding.backend_id).installed_version or None
    except Exception:  # noqa: BLE001 — an unknown backend is unmeasured, not fatal
        return None


def usage_to_dict(usage: Any) -> Optional[dict]:
    """One `Usage` sample as JSON-able fields, `None` preserved throughout (G6).

    `None` is copied rather than defaulted at every level: a window whose `used_pct` the
    driver does not report must read back as "not measurable", and a zero written here
    would later be indistinguishable from a genuinely idle quota.
    """
    if usage is None:
        return None
    split = getattr(usage, "token_split", None)
    document = {
        "context_used_pct": getattr(usage, "context_used_pct", None),
        "context_total_input_tokens": getattr(usage, "context_total_input_tokens", None),
        "model": getattr(usage, "model", None),
        "updated_at": getattr(usage, "updated_at", "") or "",
        "session_id": getattr(usage, "session_id", "") or "",
        "token_split": None if split is None else {
            "fresh_input": getattr(split, "fresh_input", None),
            "cache_read": getattr(split, "cache_read", None),
            "cache_write": getattr(split, "cache_write", None),
            "output": getattr(split, "output", None),
        },
        "windows": {
            str(int(minutes)): {
                "used_pct": getattr(window, "used_pct", None),
                "resets_at": getattr(window, "resets_at", None),
            }
            for minutes, window in (getattr(usage, "windows", None) or {}).items()
        },
        # Where the sample came from and why it is empty, when it is (per-step quota
        # telemetry): the pilot export trusts only a fresh account read.
        "source": getattr(usage, "source", None),
        "error": getattr(usage, "error", None),
    }
    if hasattr(usage, 'observed_at'):
        document.update(observed_at=usage.observed_at,
                        staleness_bound=usage.staleness_bound, pool_id=usage.pool_id)
    for name in ('pools', 'scoped_windows'):
        groups = getattr(usage, name, None)
        if groups:
            document[name] = {scope: {
                str(int(minutes)): {'used_pct': window.used_pct, 'resets_at': window.resets_at}
                for minutes, window in windows.items()}
                for scope, windows in groups.items()}
    return document


def read_usage(routing: DispatchRouting, binding: Any) -> Optional[dict]:
    """`routing.usage_reader(binding)`, serialized. A reader that raises reads as `None`:
    telemetry is never allowed to fail a dispatch it only observes.

    `read_at` is when the controller took the sample, beside the sample's own
    `updated_at`: the gap is how stale it was at claim or settle.
    """
    from maestro.control.models import now_iso

    try:
        document = usage_to_dict(routing.usage_reader(binding))
    except Exception:  # noqa: BLE001
        return None
    if document is not None:
        document["read_at"] = now_iso()
    return document


def read_attempt_tokens(routing: DispatchRouting, binding: Any, workspace: Any) -> Optional[dict]:
    """`routing.token_reader(binding, workspace)`, or `None` — never raises (G6)."""
    try:
        tokens = routing.token_reader(binding, workspace)
    except Exception:  # noqa: BLE001 — telemetry never fails the settle it observes
        return None
    return dict(tokens) if isinstance(tokens, Mapping) else None


def reset_crossing(start: Optional[Mapping], settle: Optional[Mapping]) -> Optional[int]:
    """1 if a quota window reset between the two samples, 0 if provably none did, else `None`.

    Three-valued on purpose. `0` is a measurement — both ends reported windows and none
    of them rolled over — while `None` says the question could not be answered here, and
    collapsing the two would turn an unread quota into evidence of continuity.
    """
    if not start or not settle:
        return None
    start_windows = start.get("windows") or {}
    settle_windows = settle.get("windows") or {}
    if not start_windows or not settle_windows:
        return None
    observed = False
    for minutes, opening in start_windows.items():
        closing = settle_windows.get(minutes)
        if closing is None:
            continue
        before, after = opening.get("resets_at"), closing.get("resets_at")
        if before is None:
            continue
        observed = True
        # The window the attempt opened under has rolled over when its own reset moment
        # is now in the past, which the driver reports as a *later* `resets_at`. Later by
        # more than jitter: Claude's usage endpoint reports one window's reset a second
        # apart between reads, and a real rollover moves it by the window's length.
        if after is not None and int(after) - int(before) > RESET_JITTER_SEC:
            return 1
    return 0 if observed else None


def load_pace_history(store: Any) -> tuple[dict, ...]:
    """Read attempts with one authoritative verified verdict, including amendments.

    Terminal attempt status describes execution, not verified acceptance. The
    append-only outcome table uses the same recorded_at/rowid ordering as
    load_statistics; a later controller amendment replaces only the conclusion.
    Read it on every resolution so neither restart nor a cached verdict can hide
    a reopened outcome. Keep pool activity rows without verdicts for burn overlap.
    Project only the evidence columns, avoiding unrelated payloads. This is still
    linear in retained attempts: a last-N cap could erase unknown pool activity
    or an authoritative amendment to an older attempt, so no such cap is applied.
    """
    rows = store.conn.execute('''SELECT a.attempt_id, a.run_id, a.node_id, a.status,
        a.started_at, a.binding_json, a.shape_key_json, a.wall_time_sec,
        a.usage_start_json, a.usage_settle_json, a.reset_crossing,
        o.success AS verified_success,
        o.outcome_id AS verified_outcome_id, o.verified_by AS verified_by,
        o.amends AS verified_amends, o.recorded_at AS verified_recorded_at
        FROM attempts AS a LEFT JOIN routing_outcomes AS o ON o.rowid = (
            SELECT latest.rowid FROM routing_outcomes AS latest
            WHERE latest.attempt_id = a.attempt_id
            ORDER BY latest.recorded_at DESC, latest.rowid DESC LIMIT 1)
        ORDER BY a.rowid''')
    result = []
    for row in rows:
        document = dict(row)
        if document['verified_success'] is not None:
            document['verified_success'] = bool(document['verified_success'])
        result.append(document)
    return tuple(result)


def load_statistics(store: Any, *, minimum_observations: int = 5) -> OutcomeStatistics:
    """Fold the `routing_outcomes` table into the statistics the router ranks with.

    One verdict per attempt: rows are read oldest-first and an amendment replaces the row
    it amends, so a reopened outcome is counted once, as its latest verdict — the table
    keeps the history, the statistics keep the conclusion.
    """
    statistics = OutcomeStatistics(minimum_observations=minimum_observations)
    try:
        rows = store.routing_outcomes()
    except Exception:  # noqa: BLE001 — a store without the table yields no statistics
        return statistics
    latest: dict[str, tuple[str, bool]] = {}
    for row in rows:
        latest[row["attempt_id"]] = (row["shape_key_json"], bool(row["success"]))
    for shape_json, success in latest.values():
        key = shape_key_from_json(shape_json)
        if key is not None:
            statistics.record(key, success=success)
    return statistics


def record_outcome(routing: DispatchRouting, shape_key: Any, *, success: bool) -> None:
    """Fold one controller-verified outcome into this routing's statistics.

    Goes through `router.record_outcome` rather than touching `OutcomeStatistics`
    directly, so the shape a run is credited to is decided by the same function the
    router itself uses. The shape is read back from the attempt row, which is why this
    takes a `ShapeKey` and presents it as the binding's own.
    """
    from types import SimpleNamespace

    if shape_key is None:
        return
    router.record_outcome(
        routing.statistics, SimpleNamespace(shape_key=shape_key), success=success
    )


def binding_view(document: Optional[str]):
    """The stored `binding_json` as an object a `usage_reader` can be handed at settle.

    The attempt row outlives the in-memory `AgentBinding`, and settle may happen in a
    later controller process. Rather than half-rebuild a frozen dataclass from a dict
    that was never meant to reconstruct one, this presents the fields a reader actually
    consults — which is what makes the settle-side sample readable after a restart.
    """
    import json
    from types import SimpleNamespace

    if not document:
        return None
    try:
        body = json.loads(document)
    except (TypeError, ValueError):
        return None
    return SimpleNamespace(
        backend_id=body.get("backend_id", ""),
        model_id=body.get("model_id", ""),
        usage_pool_id=body.get("usage_pool_id", ""),
        node_id=body.get("node_id", ""),
        binding_id=body.get("binding_id", ""),
    )


def shape_key_from_json(document: Optional[str]):
    """Rebuild a `ShapeKey` from its stored form, or `None` when it cannot be read."""
    import json

    from maestro.backends.catalog import ShapeKey

    if not document:
        return None
    try:
        body = json.loads(document)
        return ShapeKey(
            backend_id=body["backend_id"], model_id=body["model_id"],
            effort=body["effort"], role_version=body["role_version"],
            task_class=body["task_class"], template_id=body["template_id"],
            template_version=body["template_version"],
            active_fragment_set=tuple(body.get("active_fragment_set") or ()),
        )
    except Exception:  # noqa: BLE001
        return None


def render_binding_argv(
    *,
    backend_id: str,
    runtime_kind: str,
    plan: Any,
    manifest_artifact_id: Optional[str],
    session: Optional[SessionDecision],
) -> list[str]:
    """The worker-argv tail that carries one binding.

    Effort args use the `--effort-arg=<a>` spelling: the values are themselves flags
    (`--effort`), and argparse would read a bare `--effort-arg --effort` as a missing value.
    """
    argv = [
        "--backend-id", str(backend_id),
        "--runtime-kind", str(runtime_kind),
        "--model", str(plan.model),
    ]
    argv += [f"--effort-arg={arg}" for arg in plan.effort_args]
    argv += [f"--effort-config={key}={value}" for key, value in plan.effort_config]
    if manifest_artifact_id:
        argv += ["--manifest-artifact-id", str(manifest_artifact_id)]
    if session is not None:
        argv += ["--session-id", session.session_id, "--session-mode", session.mode]
    return argv


def endpoint_from_config(item: Mapping[str, Any]):
    """One `config.free_tier_endpoints()` row as a `ModelEndpoint`."""
    from maestro.backends.model_api import ModelEndpoint

    body = {
        key: item[key] for key in ("endpoint_id", "base_url", "model", "api_key_env")
    }
    if item.get("usage_pool_id"):
        body["usage_pool_id"] = item["usage_pool_id"]
    return ModelEndpoint(**body)


def endpoints_for_catalog(items: Sequence[Mapping[str, Any]]) -> list:
    """`model_api.capability_entry` for each configured endpoint, at its verified version."""
    from maestro.backends.model_api import capability_entry

    entries = []
    for item in items:
        extra = {"strength": item["strength"]} if item.get("strength") else {}
        entries.append(
            capability_entry(
                endpoint_from_config(item), verified_version=item["verified_version"], **extra
            )
        )
    return entries
