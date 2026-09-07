# Local production delivery contract

This contract governs this host's manual worktree/runtime deliveries. The owner
is the local operations scripts, not the agent loop. Upstream
`hermes_cli.update_inventory` and `update_receipt` remain the runtime inventory
and updater receipt owners. This tool does not invoke `hermes update` or replace
its restart orchestration.

## Mandatory gates (all required)

1. Prove live LaunchAgent, PID, interpreter, worktree and source HEAD. Export
   upstream runtime inventory. Never infer the live source from the current cwd.
2. Enumerate the affected source roots and external plugin/editable roots.
   `hermes_delivery.py capture --root ROOT ... --output NEW_PRIVATE_BUNDLE`
   preserves actual selected source bytes, including dirty files, by SHA-256.
   `verify BUNDLE` checks both the source and backup blobs. Preserve unrelated
   dirty state; never stash/reset or commit it to make a release look clean.
3. Separately back up launch/config files privately, interpreter symlinks and
   pyvenv.cfg. Keep the old interpreter installed. Source capture is NOT a
   full machine, venv, configuration, Memory or database backup. Its extension
   allowlist and directory exclusions must be reviewed for each delivery.
   Missing required roots, ignored runtime assets or unknown provenance block
   the delivery; an empty inventory is not success.
4. `probe --python PYTHON --worktree WORKTREE` records the actual interpreter,
   linked SQLite, distribution versions and the accepted local memory.vault
   mapping. No dependency installation is performed.
5. For a runtime-only SQLite repair, use `accept-runtime --bundle BUNDLE
   --before BEFORE_JSON --python CANDIDATE --worktree WORKTREE
   --require-fixed-sqlite`. Source bytes, Python version and installed package
   versions must match. This is a component gate, not whole-delivery PASS.
   For code upgrades, source differences require explicit per-owner review and
   corresponding tests; do not simply replace the baseline until it is green.
6. Execute existing focused tests against the actual candidate interpreter:
   update inventory/receipt, lazy dependencies, Vault plugin and evaluator
   tests using isolated fixtures. Record commands, exit codes and counts.
   A skip is not a pass. Never substitute `gateway running` or doctor output.
   The shell runner can silently fall back when the production venv lacks
   pytest: inspect its selected interpreter. In that case use its existing
   run_tests_parallel.py with the explicit production Python and an isolated,
   version-pinned test-only PYTHONPATH, never install pytest into production.
7. Only then switch the affected runtime using its current supervisor. Record
   PID before/after, runtime identity, connections and fresh error evidence.
   Do not modify database journal mode, schema, retrieval or unrelated services.
8. Recheck source preservation, dependency parity, focused tests and the
   existing retrieval evaluation. Preserve its case/precision/negative gates.
   Record unchanged failures separately; do not call a baseline failure fixed.
9. On failed startup/provenance/contract validation restore saved runtime
   pointers/configuration and use the same supervisor. Never repair data to
   make runtime acceptance pass. Keep both the failure and rollback evidence.

## Receipt and rollback evidence

Store each delivery under a private dated reports directory: source bundle,
upstream inventory, before/candidate/after probes, config/pointer backups,
commands and test logs, and final outcome. PASS requires every applicable gate;
UNKNOWN/PENDING blocks cutover. The source bundle does not auto-restore files:
verify the blob hash and restore only reviewed affected paths with original
metadata. No broad source restore over unrelated changes.

This is a local CLI and acceptance procedure, not a daemon, service, new
registry, LLM router or new dependency manager. It adds no per-turn runtime hop.
