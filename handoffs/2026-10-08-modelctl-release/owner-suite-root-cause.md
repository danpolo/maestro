# Owner suite 195/2 -> 198/0 (2026-10-08)

Both failures came from tests coupled to the host's real Maestro checkout. Session13 passed
only because `/home/dan/projects/maestro` did not yet contain the D-A–D-E code; after
integration (0c7eb92) it did. Host hook files were unchanged since 2026-09-30, and the
session9 owner candidate differs from the repo only in whitespace.

1. `test_unknown_inventory_source_refuses_deletion`: a **real modelctl defect**.
   `transitions.maestro_inventory` ran `python -I -c "sys.path.insert(0, src); from maestro..."`.
   With a missing `src`, the interpreter's own editable install (`.venv` ->
   main checkout) answered, so pruning went ahead on another checkout's inventory.
   Fix: the subprocess refuses unless `maestro.model_inventory` resolves under `src`.
2. `test_scan_auto_registers_existing_family_and_keeps_old_rows`: a **non-hermetic test**.
   The env fixture's `maestro_src` is the real checkout, which now declares
   `CLASS_TRANSITION_NOTICE_VERSION = 1`, so Maestro (correctly) owns the review notice and
   modelctl stays silent. Fix: the test pins `maestro_owns_switch_notice` and is
   parametrized over both consumers (+1 test, hence 198).

modelctl commit ccc0b2f. Logs: `gate-owner-0c7eb92.log` (before), `gate-owner-0c7eb92-after.log`
(main checkout), `gate-owner-release-0c7eb92.log` (released path). Runner: `run-owner-gate.sh`.
No Maestro code change; release SHA stays 0c7eb92.
