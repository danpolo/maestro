"""Pair effort delivery for fresh Codex launches.

Kept apart from the runtime/telemetry driver, which is owned by parallel work. The
driver's existing launch and resume lifecycle stays in CodexBackend.
"""
from dataclasses import replace
from maestro.backends.codex import CodexBackend


class CodexLaunchBackend(CodexBackend):
    @staticmethod
    def _with_effort(argv, spec):
        controls = [str(arg) for arg in spec.effort_args]
        for key, value in spec.effort_config:
            controls += ["-c", f"{key}={value}"]
        return argv[:-1] + controls + argv[-1:]

    def launch_argv(self, spec):
        return self._with_effort(super().launch_argv(spec), spec)

    def resume_argv(self, spec, thread_id, prompt):
        return self._with_effort(super().resume_argv(spec, thread_id, prompt), spec)

    def _resolve_spec(self, handle, prompt, spec):
        resolved = super()._resolve_spec(handle, prompt, spec)
        if spec is not None:
            return resolved
        meta = self.read_launch_meta(handle.workspace)
        if meta is None:
            return replace(resolved, effort_args=self._spec.effort_args,
                           effort_config=self._spec.effort_config)
        return replace(resolved, effort_args=tuple(meta.get("effort_args", ())),
                       effort_config=tuple(tuple(item) for item in meta.get("effort_config", ())))
