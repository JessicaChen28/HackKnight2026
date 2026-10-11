# Phase 1 security and reliability audit

Audit and tests: October 10, 2026. Scope: local Azgaar generation, Minimal JSON download, parser, shared RDP coastline processing, and storage. No gameplay, credentials, UI, or server added.

## Findings and changes

| Finding in the original implementation | Implemented safeguard |
| --- | --- |
| Names were sanitized, but path ancestors could be symlinks/junctions; Unicode controls and length were not fully handled. | `safe_storage.py` checks path containment and all existing ancestors, rejects symlinks/reparse points, normalizes names, removes separators/control characters/dot traversal, handles Windows devices, and bounds filename length. The original display name stays in metadata. |
| JSON loading was unbounded and required metadata, IDs, coordinates, and references were only partially validated. | Bounded file reads with file identity check; reject duplicate keys, invalid syntax, nonfinite numbers, and excessive depth/structure. Validate required metadata, collection types, unique IDs, finite bounded coordinates, settlement/state/feature/culture references, state capitals, and present route feature references. Optional fields remain optional. |
| Outputs were written directly and separately, leaving a possible partial world after interruption. | Same-directory temporary JSON writes, flush/fsync, atomic hard-link publication that cannot overwrite files, then reload validation. Generate all files in a unique staging directory and publish the whole completed directory with an atomic no-replace rename. `complete.json` marks completion. Failed offline processing rolls back its own products; `.incomplete.json` distinguishes interruption. |
| Browser navigation/popups were unrestricted, and Playwright's sandbox option was not explicitly enabled. | HTTPS Azgaar application navigation allowlist, origin checks, popup closure, dismissed dialogs, sandbox enabled, empty permissions, blocked service workers, fresh nonpersistent contexts. Necessary subresources remain permitted. Browser/driver inherit an environment allowlist rather than application secrets. |
| Some synchronous Playwright calls and native GEOS processing could hang beyond operation timeouts. | One supervised process retains the existing synchronous browser flow. Parent enforces stage and total deadlines even during download completion, JS extraction, or GEOS work. A cancellation event and Ctrl+C stop the process tree. psutil tracks descendants and terminates/kills them during cleanup. |
| Repeated calls could launch concurrent browsers without a bound. | One active call per host process; a kernel file lock prevents CLI processes from using the same export root concurrently. RSS and download-size watchdogs bound sustained resource use. |
| Importing with missing dependencies exited the host interpreter. | Normal imports and ordinary exceptions; only CLI entry points exit. Configurable export root, cancellation event, progress callback, and returned validated export path support a future UI. |
| PowerShell setup could report success after a native command failed and permanently changed the shell directory. | Check native exit codes; restore location in `finally`. Added ignores for local secrets and temporary artifacts. Existing intentional compressed fixtures remain tracked. |

Existing completion events, exact Minimal JSON selector, `expect_download`, byte-preserved original export, schema/IDs, coordinate extraction from actual cells/vertices, and shared-boundary RDP algorithm were retained. `download.failure()` and completed `download.path()` now precede copying and parsing. Unexpected downloads are cancelled. Invalid-world replacements remain bounded, with diagnostics separate under `.failed/`; infrastructure failures are not retried as invalid geography.

The single supervised worker is the main architectural change. Keeping everything in the UI process would leave blocked synchronous downloads/evaluation and native geometry calls without reliable cancellation. One disposable process supplies hard execution deadlines without changing geometry algorithms or introducing a scheduler.

## Defaults and compatibility

- Input: 64 MiB per file, configurable through `--max-input-mb` / `max_input_bytes`.
- JSON: depth 100 and 2,000,000 values; 100,000 records per entity collection; source geometry at most 100,000 cells and 1,000,000 ring vertices (the JSON structure limit can apply first).
- Map dimensions: positive and at most 32,768 map units. Settlements and output shores must remain within the rectangle. Raw Voronoi vertices may extend outside it, up to 16 times the relevant dimension, before the existing clipping step.
- Operation/stage timeout: 180 seconds. Total worker deadline: 300 seconds including retries; separate navigation/export/geometry overrides available. Cleanup adds a few seconds.
- Memory: 2,048 MiB combined worker/descendant RSS, configurable through `--max-memory-mb` / `max_memory_mb`.
- Invalid-world retry count: 5, configurable from 1 to 20. Source inconsistency fails validation; no fabricated geography is inserted.
- Required: Azgaar metadata name/version/seed/dimensions, pack, usable settlements/states/features, valid coordinates and relationships. Optional: absent ancillary collections and allowlisted optional fields. Present references cannot dangle. Map-edge water with no original feature ID remains explicitly identified by `feature_id: null`, with a diagnostic note.
- Completed output schema remains version 1. Older files can be loaded with `validate_outputs`; the new `complete.json` manifest is required for newly generated worlds. The Python API still returns `azgaar_minimal.json`; `.parent` gives the completed directory.
- Dependencies: `playwright==1.63.0`, `shapely==2.2.0`, and new `psutil==7.2.2`. Install with `python -m pip install -r requirements.txt`, then `python -m playwright install chromium`. Python 3.10+ required. Phase 1 needs no `.env` or API keys.

## Executed verification

On Windows with Python 3.12:

- `python -m unittest discover -s tests -v`: **44 passed, 1 optional live test skipped** (45 discovered). Covers traversal/device/Unicode names, directory/file collisions, actual Windows junction rejection, kernel lock release, malformed/empty/oversized/deep/nonfinite JSON, missing data, duplicate IDs, invalid coordinates/references, accepted missing optional fields, and a clear CLI message when dependencies are absent.
- Failure tests exercise simulated download cancellation, generation timeout, extraction errors, parser errors, disk-full serialization and multi-file writes, preservation of previous worlds, staging cleanup, and release of the in-process lock. Actual spawned workers exercise cancellation, stage/total deadlines, memory-budget failure, and termination of a descendant process.
- Existing real-world fixtures still verify stable IDs/routes, closed valid polygons, lake holes, populated islands, settlement containment, coast crossings, complete shared coverage, reduced-tolerance fallback, and independent reload. Frepoyland retained its prior **4,614 → 1,796 vertices (61.07%)**.
- Opt-in real Azgaar integration was executed successfully twice. **Chetrov:** 799 settlements, 22 states, 19 landmasses, 6 water bodies; **4,033 → 1,491 vertices (63.03%)**. After the RSS watchdog was added, **Ala:** 516 settlements, 16 states, 22 landmasses, 1 water body; **5,413 → 1,997 vertices (63.11%)**. Both downloaded original Minimal JSON through Playwright, extracted geometry during the same session, validated/reloaded outputs, published a completed directory, and left no staging directory. The tests intentionally removed their temporary worlds afterward. A later direct CLI run with the project virtual environment produced **Courcot** (433 settlements, 18 states, **4,399 → 1,633 vertices; 62.88%**) under the project's `exports/` directory.
- `python -m pip check`: passed. Generator `--help`: passed. PowerShell setup syntax parsed without errors; the entire installer was not rerun against a fresh machine.

## Remaining limits

- Tested on Windows only. Linux/macOS no-replace rename and process cleanup implementations still need platform testing. NTFS/POSIX hard links and atomic rename support are required; unsupported filesystems fail explicitly rather than fall back to overwriting.
- RSS and download-size limits are polled; rapid spikes can exceed thresholds temporarily. RSS can double-count shared pages. These are practical local resource controls, not OS-enforced CPU/memory/disk quotas or a hostile-code sandbox. Chromium's sandbox remains enabled.
- Storage checks reject existing symlinks/junctions but are not a defense against another malicious process running as the same OS user and racing directory replacements. Choose a private, writable storage location. Broad reparse-point rejection can reject cloud-placeholder paths; use a plain local export root in that case.
- Abrupt termination of the host, power loss, or OS denial of process termination can bypass cleanup. Such staging directories stay hidden under `.staging-*`, separate from completed worlds. Kernel locks release when their owner exits. After verifying no generation is running, leftover staging directories may be removed manually. No automatic deletion of unknown older staging folders is performed. Atomic publication protects visibility, not guaranteed power-loss durability on every storage device.
- Rejected-world diagnostics under `.failed/` are retained intentionally and need occasional manual cleanup; repeated separate invocations can accumulate them. They may contain an incomplete source capture and must never be treated as playable worlds.
- Concurrency is limited per host process and per export root. Independent applications deliberately using different roots can still run simultaneously.
- Direct offline parser functions are synchronous and have no hard deadline; use the supervised generator for UI generation, and keep direct parsing off the UI thread. Progress callbacks must return promptly so the supervisor can keep monitoring.
- Azgaar remains a live third-party dependency. Upstream selectors/schema, site availability, browser vulnerabilities, and inconsistent generated geography cannot be guaranteed away. Chromium and pinned dependencies need periodic updates. No comprehensive third-party vulnerability audit or malicious-browser exploit testing was performed.
