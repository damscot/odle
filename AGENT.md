# AGENT.md

## Scope
This repository is built around two Python programs:

- `search_disk_fault.py`: analyzes block readability by LBA, persists status as compact JSONL segments.
- `recover_disk.py`: reads that JSONL status and computes or applies an LVM2 partition/PV/VG recovery plan.

No legacy program names should be used.

## Global Architecture

End-to-end workflow:

1. `search_disk_fault.py` scans target ranges and writes `sector_status*.jsonl`.
2. `recover_disk.py` ingests the JSONL, filters/expands unsafe zones, and computes usable ranges.
3. In plan mode, `recover_disk.py` prints preflight state and exact commands.
4. In apply mode, `recover_disk.py` executes `sfdisk`, `partprobe`, `pvcreate`, and optionally `vgcreate`.

Data contract between both tools:

```json
{"begin":160312320,"sectors":2048,"updated_at":"2026-06-07T21:27:21+02:00","globstatus":"P"}
```

Required keys:
- `begin` (absolute LBA)
- `sectors` (count in 512-byte sectors)
- `updated_at` (ISO-like timestamp)
- `globstatus` in `U|P|F`

## Detailed Architecture: search_disk_fault.py

### Core constants and model
- `UNKNOWN`, `PASS`, `FAIL`: internal numeric status codes.
- `STATUS_TO_NAME`, `NAME_TO_STATUS`, `CHAR_TO_STATUS`, `STATUS_TO_CHAR`: conversion maps.
- `Segment` dataclass: immutable segment with `begin`, `sectors`, `status`, `updated_at`.

### Utility and parsing functions
- `latest_updated_at(ts_a, ts_b)`
   - Goal: preserve the most recent timestamp when merging segments.
   - Behavior: tries `datetime.fromisoformat`, falls back to lexical compare.

- `now_iso()`
   - Goal: generate current timestamp for segment updates.

- `is_block_device(path)`
   - Goal: validate device path is a block device.

- `get_device_start_lba(device, base_lba_arg)`
   - Goal: compute partition/device start for absolute/device LBA mapping.

- `get_parent_disk_device(device)`
   - Goal: map partition device to parent disk when needed.

- `get_device_size_sectors(device)`
   - Goal: read sector count via `blockdev --getsz`.

- `build_macros(device, base_lba_arg)`
   - Goal: expose macros used in CLI expressions:
      - `disk_start`, `partition_start`, `disk_size`, `partition_size`.

- `safe_eval_int(expr, macros)`
   - Goal: evaluate integer expressions safely with AST whitelist.

- `resolve_target_value(value, macros)`
   - Goal: parse plain integer or expression/macro value.

- `map_lba(begin_lba, lba_mode, device_start)`
   - Goal: map absolute to device-relative addressing when required.

- `run_dd_read(...)`
   - Goal: execute one `dd` read probe for a chunk.
   - Notable behavior:
      - adaptive `bs` when `--dd-bs-bytes=0`
      - timeout handling
      - progress passthrough with `--dd-progress`
      - logs all commands and failures.

### StatusStore class
Goal: maintain normalized status segments in memory and on disk.

Major methods:
- `__init__`, `_load_or_init`
   - Load existing JSONL or initialize full target as `UNKNOWN`.

- `_normalize_all_segments(segments)`
   - Sort and merge contiguous same-status segments.
   - Keeps latest `updated_at` across merges.

- `_slice_segments(from_lba, to_lba)`
   - Build window view from global segments.

- `_refresh_window()`
   - Refresh current target window.
   - Auto-fills gaps inside the window as `UNKNOWN` segments.

- `_load_segments(raw)`, `_segments_from_doc(doc)`
   - Parse JSONL docs; only compact format supported.
   - Rejects legacy multiline-status formats.

- `flush()`
   - Atomic write to `*.tmp` then `os.replace`.

- `get(lba)`, `set_range(from_lba, to_lba, status)`
   - Query/update range status; updates timestamps only on changed range.

- `_merge_adjacent(segments)`
   - Merge helper preserving latest timestamp.

- `chunk_has_status`, `chunk_needs_test`, `chunk_original_statuses`, `_overlapping_segments`
   - Chunk-level selection and status diff utilities.

- `counts()`
   - Returns `unknown/pass/fail` counts on current window.

- `export_text`, `import_text`
   - Optional text import/export compatibility path.

### Scan orchestration
- `scan_target_range(args)`
   - Validates and resolves target range.
   - Loads `StatusStore` window.
   - Chooses scan statuses:
      - default: `UNKNOWN`
      - retest modes: `PASS` and/or `FAIL` based on flags.
   - Iterates only matching segments (optimized).
   - Executes `dd` by chunks.
   - Writes `PASS`/`FAIL` results and logs transitions.

- `parse_args()`
   - Validates CLI consistency and numeric constraints.

- `main()`
   - Entry point, delegates to `scan_target_range`.

## Detailed Architecture: recover_disk.py

### Core constants and command registry
- Size constants: `SECTOR_SIZE`, `MIB_SECTORS`, `MIN_PV_MIB`, `BIG_PV_MIB`, `DEFAULT_GUARD_MIB`.
- Status constants: `STATUS_PASS`, `VALID_STATUS`.
- `TOOL_PATHS`: resolved absolute command paths for deterministic execution.

### Command execution and preflight functions
- `_confirm_or_abort(cmd, interactive)`
   - Goal: per-command confirmation gate in apply mode.

- `run_cmd(...)`
   - Goal: centralized subprocess execution.
   - Features: optional echo, capture, interactive confirmation, inline input.

- `verify_required_tools(for_apply, for_vgcreate)`
   - Goal: hard-required binary checks and path hydration.

- `collect_tool_status(include_vgcreate)`
   - Goal: produce present/missing utility report for plan recap.

- `tool_cmd(name, *args)`
   - Goal: construct command using resolved absolute path when available.

### JSONL loading and range math
- `status_char(value)`
   - Validate `globstatus` values.

- `load_segments_jsonl(path)`
   - Parse and normalize compact JSONL status segments.

- `merge_ranges(ranges)`
   - Merge overlapping/adjacent blocked intervals.

- `invert_ranges(cover_start, cover_end, blocked)`
   - Compute usable complement intervals.

- `align_range(start, end, align)`
   - Align to MiB boundaries.

- `sectors_to_mib(sectors)`
   - Convert sectors to MiB.

- `pe_for_pv(sectors)`, `pick_vg_pe(pv_ranges)`
   - PE policy logic (4M if large-only, else 1M).

### Device and partition helpers
- `partition_path(device, number)`
   - Handle `/dev/sdX1` vs `/dev/nvme0n1p1` naming.

- `get_device_pttype(device)`
   - Read current partition table type via `lsblk`.

- `get_device_total_sectors(device)`
   - Read disk size via `lsblk -b`.

- `clip_ranges_to_bounds(ranges, lower, upper)`
   - Enforce GPT usable bounds.

- `get_existing_partition_count(device)`
   - Count existing partitions for safety gate.

- `create_sfdisk_script(ranges, pttype)`
   - Emit complete sfdisk script including `label` and LVM partition type.

### Apply-stage helpers
- `apply_partitioning(device, ranges, pttype, dry_run, interactive)`
   - Applies sfdisk script with wipe options.
   - Runs `partprobe` after write.

- `apply_pvcreate(device, start_part_num, count, dry_run, interactive)`
   - Creates PVs for generated partitions.

- `apply_vgcreate(vg_name, pe_size, pv_paths, dry_run, interactive)`
   - Creates VG from generated PVs.

### Program entry and orchestration
- `parse_args()`
   - CLI options and validation.

- `main()` sequencing
   1. Parse args and derive `interactive_apply`.
   2. Collect utility status and required sets.
   3. Validate plan prerequisites.
   4. Load status JSONL and compute blocked/usable ranges.
   5. Clip for GPT bounds and align ranges.
   6. Print structured plan sections:
       - Disk status
       - Planned partitions
       - Utilities
       - Commands to execute
   7. Enforce safety guard: abort if existing partitions > 0.
   8. If plan-only: print manual next steps and exit.
   9. If apply: verify apply prerequisites, run partitioning/PV/VG steps.
   10. Print final manual LV/ext4 guidance.

## Error Handling Strategy

### search_disk_fault.py
- CLI validation errors: `argparse` errors.
- Runtime validation: `RuntimeError`/`IndexError` for malformed status data or out-of-bounds operations.
- Device failures: explicit non-zero return codes from `scan_target_range`.
- `dd` failures/timeouts: logged and converted into FAIL status updates.

### recover_disk.py
- Missing tools: early `RuntimeError` with explicit missing list.
- JSONL schema errors: explicit parse/runtime errors.
- Device introspection failures: early return code `2`.
- Existing partition detection: hard stop with return code `3`.
- Apply command failures: wrapped to return code `4` with contextual message.

## Intermediate Files and Operational Artifacts

Primary intermediate artifact shared across programs:
- `sector_status_disk.jsonl` (or equivalent `--status-file` path).

Additional artifacts created by search phase:
- scan log file (default `test_ranges_python.log`).

Recover phase is mostly command-driven:
- prints plan and commands
- does not create LV or filesystem itself
- relies on system tools for on-disk changes.

## Agent Contribution Rules

- Keep names and docs aligned with current binaries:
   - `search_disk_fault.py`
   - `recover_disk.py`
- Do not reintroduce references to legacy script names.
- Preserve the hard safety stop on existing partitions unless explicitly requested.
- Preserve plan-mode read-only behavior.
- If command set changes, update:
   - utility checks
   - plan command list
   - README and AGENT documentation.
