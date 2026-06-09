# Optimistic Disk Lifespan Extender (ODLE)

The objective of this toolkit is to recover and repurpose storage devices (HDD, SSD, NVMe) that exhibit read faults or intermittent disconnections by excluding defective logical block address (LBA) ranges. It focuses on maximizing usable capacity (for example, recovering ~95% of the device) while isolating and excluding defective regions that would otherwise cause I/O failures.

Rather than attempting hardware repairs or recovering data contents, the tools perform read-only scans and generate a compact JSONL status map that classifies sectors as usable or unreadable. The recovery planner consumes this map to propose an LVM2-oriented partitioning strategy: select contiguous, MiB-aligned ranges of good sectors, create Physical Volumes (PVs) from those ranges, and optionally combine them into a Volume Group (VG) so one or more Logical Volumes (LVs) can be provisioned for reuse.

These scripts perform raw device access and can be destructive if misused. Back up any important data and run them only on spare or non-critical systems. Using a USB-to-SATA/adapter is recommended to allow easy power-cycling if the drive disconnects. Note that these tools were generated with AI and have not undergone exhaustive review—verify outputs and exercise caution before applying any destructive operations.

The toolkit comprises two components:

1. Scanning disk LBAs and recording block status in compact JSONL segments.
2. Building and optionally applying an LVM2 recovery partition plan from that JSONL.

## Programs

## search_disk_fault.py

Analyzes block readability over a target LBA range and writes status as compact JSONL segments.

Main behavior:
- supports expression/macros for ranges (`disk_start`, `partition_start`, `disk_size`, `partition_size`)
- scans by chunks with `dd`
- supports retest modes (`--retest-pass`, `--retest-fail`)
- stores status as merged JSONL segments (`U`, `P`, `F`)
- keeps per-segment timestamps (`updated_at`)

Typical output files:
- status file (default `sector_status.jsonl`, often `sector_status_disk.jsonl`)
- log file (default `test_ranges_python.log`)

### Key options (search)
- `device`
- `--lba-mode {absolute,device}`
- `--target-begin`, `--target-sectors` or `--target-end`
- `--scan-sector-count`
- `--dd-progress`
- `--dd-bs-bytes`
- `--retries`, `--timeout-sec`
- `--retest-fail`, `--retest-pass`
- `--status-file`, `--log-file`

## recover_disk.py

Reads the JSONL status produced by `search_disk_fault.py` and builds an LVM2-oriented recovery plan.

Main behavior:
- only `P` sectors are considered usable
- `F` and `U` sectors are excluded and expanded by guard margin
- computes usable ranges, aligns to MiB boundaries, enforces minimum partition size
- prints structured plan recap:
	- disk status
	- utility presence/missing
	- exact commands to run
- apply mode executes:
	- `sfdisk` (with wipe options)
	- `partprobe`
	- `pvcreate`
	- optional `vgcreate`

Safety defaults:
- plan mode is read-only and non-privileged
- destructive actions require `--apply`
- apply mode is interactive by default (per command)
- `--no-interactive` disables confirmations
- aborts if disk already contains partitions

### Key options (recover)
- `status_file`
- `--device`
- `--guard-mib` (default 1)
- `--min-pv-mib` (default 8)
- `--apply`
- `--create-vg`
- `--vg-name` (default `recdiskvg`)
- `--no-interactive`

## Data Handoff: search -> recover

The bridge between both tools is the compact JSONL status file.

Example line:

```json
{"begin":160312320,"sectors":2048,"updated_at":"2026-06-07T21:27:21+02:00","globstatus":"P"}
```

Pipeline:

1. `search_disk_fault.py` writes/updates status JSONL segments.
2. `recover_disk.py` loads the same file.
3. Recover planner filters usable `P` areas and plans LVM2 partitions/PVs.

## Usage Examples

## 1) Run scan and create status map

```bash
sudo python3 search_disk_fault.py /dev/sde \
	--lba-mode device \
	--target-begin disk_start \
	--target-sectors disk_size \
	--scan-sector-count 524288 \
	--dd-progress \
	--status-file sector_status_disk.jsonl \
	--log-file test_ranges_python.log
```

## 2) Show recovery plan only (safe mode)

```bash
python3 recover_disk.py sector_status_disk.jsonl --device /dev/sde
```

## 3) Apply recovery plan with confirmations

```bash
sudo python3 recover_disk.py sector_status_disk.jsonl --device /dev/sde --apply
```

## 4) Apply without per-command prompts

```bash
sudo python3 recover_disk.py sector_status_disk.jsonl --device /dev/sde --apply --no-interactive
```

## 5) Apply and create VG directly

```bash
sudo python3 recover_disk.py sector_status_disk.jsonl --device /dev/sde --apply --create-vg --vg-name recdiskvg
```

## 6) Plan without create-vg and use manual steps

```bash
python3 recover_disk.py sector_status_disk.jsonl --device /dev/sde
```

The output includes manual commands for:
- `vgcreate` (when `--create-vg` is not set)
- `lvcreate`
- `mkfs.ext4`
- `mount`

## Notes for contributors

- Keep references aligned with the current tools:
	- `search_disk_fault.py`
	- `recover_disk.py`
- Keep plan mode non-destructive.
- Keep apply mode interactive by default.
- Keep command preflight checks synchronized with any new external dependency.
