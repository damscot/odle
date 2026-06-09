#!/usr/bin/env python3
"""
This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.

file authors: "damscot"

Builds an LVM-friendly partition plan from scanner JSONL output produced by
`search_disk_fault.py` tool then optionally applies it using sfdisk,
pvcreate, and vgcreate.

Supported input format (one JSON object per line):
  {"begin":0,"sectors":1048576,"updated_at":"...","globstatus":"P"}

Rules:
- Only PASS sectors are considered usable for PVs.
- FAIL/UNKNOWN sectors are excluded.
- A safety guard margin is applied around excluded ranges.
- Partition size must be >= 8 MiB.
- PE policy for VG creation:
  - partition >= 100 MiB -> 4 MiB PE candidate
  - 8 MiB < partition < 100 MiB -> 1 MiB PE candidate
  Since PE size is VG-wide, the script picks 1 MiB if any small PV exists,
  otherwise 4 MiB.

Important safety notes:
- Default mode is plan-only (no disk changes).
- Use --apply to run sfdisk/pvcreate/vgcreate.
- In --apply mode, commands are confirmed by default; use --no-interactive to disable confirmations.
- This script assumes the target disk is dedicated to this workflow.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SECTOR_SIZE = 512
MIB_SECTORS = 1024 * 1024 // SECTOR_SIZE  # 2048
MIN_PV_MIB = 8
BIG_PV_MIB = 100
DEFAULT_GUARD_MIB = 1

STATUS_PASS = "P"
VALID_STATUS = {"U", "P", "F"}
TOOL_PATHS: dict[str, str] = {}


def _confirm_or_abort(cmd: list[str], interactive: bool) -> None:
    if not interactive:
        return
    print("About to run:", " ".join(cmd))
    ans = input("Execute this command? [y/N]: ").strip().lower()
    if ans not in {"y", "yes", "o", "oui"}:
        raise RuntimeError("Command cancelled by user")


def run_cmd(
    cmd: list[str],
    dry_run: bool = False,
    capture: bool = False,
    interactive: bool = False,
    input_text: str | None = None,
    echo: bool = True,
) -> subprocess.CompletedProcess:
    if echo:
        print("$", " ".join(cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    _confirm_or_abort(cmd, interactive)
    try:
        return subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=capture,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {cmd[0]}") from exc


def verify_required_tools(for_apply: bool, for_vgcreate: bool) -> None:
    required = {"lsblk", "sfdisk"}
    if for_apply:
        required.update({"partprobe", "pvcreate"})
        if for_vgcreate:
            required.add("vgcreate")

    missing = sorted(tool for tool in required if shutil.which(tool) is None)
    if missing:
        raise RuntimeError("Missing required tools: " + ", ".join(missing))

    for tool in sorted(required):
        TOOL_PATHS[tool] = shutil.which(tool) or tool


def collect_tool_status(include_vgcreate: bool) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    tools = ["lsblk", "sfdisk", "partprobe", "pvcreate"]
    if include_vgcreate:
        tools.append("vgcreate")

    present: list[tuple[str, str]] = []
    missing: list[str] = []
    for tool in tools:
        path = shutil.which(tool)
        if path:
            TOOL_PATHS[tool] = path
            present.append((tool, path))
        else:
            TOOL_PATHS[tool] = tool
            missing.append(tool)
    return present, missing, tools


def tool_cmd(name: str, *args: str) -> list[str]:
    return [TOOL_PATHS.get(name, name), *args]


def status_char(value: str) -> str:
    text = str(value).strip()
    if not text or text[0] not in VALID_STATUS:
        raise RuntimeError(f"Invalid globstatus value: {value}")
    return text[0]


def load_segments_jsonl(path: Path) -> list[tuple[int, int, str]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(f"Empty status file: {path}")

    segments: list[tuple[int, int, str]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON at line {line_no}: {exc}") from exc

        if "begin" not in doc or "sectors" not in doc or "globstatus" not in doc:
            raise RuntimeError(
                f"Line {line_no}: required keys are begin/sectors/globstatus (new JSONL format only)"
            )

        begin = int(doc["begin"])
        sectors = int(doc["sectors"])
        if sectors <= 0:
            raise RuntimeError(f"Line {line_no}: sectors must be > 0")
        st = status_char(doc["globstatus"])
        segments.append((begin, sectors, st))

    if not segments:
        raise RuntimeError("No segments found in status file")

    segments.sort(key=lambda x: x[0])

    # Merge contiguous segments with same status.
    merged: list[tuple[int, int, str]] = [segments[0]]
    for begin, sectors, st in segments[1:]:
        mb, ms, mst = merged[-1]
        if st == mst and mb + ms == begin:
            merged[-1] = (mb, ms + sectors, mst)
        else:
            merged.append((begin, sectors, st))
    return merged


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [ranges[0]]
    for start, end in ranges[1:]:
        ls, le = out[-1]
        if start <= le + 1:
            out[-1] = (ls, max(le, end))
        else:
            out.append((start, end))
    return out


def invert_ranges(cover_start: int, cover_end: int, blocked: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if cover_start > cover_end:
        return []
    if not blocked:
        return [(cover_start, cover_end)]

    out: list[tuple[int, int]] = []
    cur = cover_start
    for bs, be in blocked:
        if be < cur:
            continue
        if bs > cover_end:
            break
        if bs > cur:
            out.append((cur, min(bs - 1, cover_end)))
        cur = max(cur, be + 1)
        if cur > cover_end:
            break
    if cur <= cover_end:
        out.append((cur, cover_end))
    return out


def align_range(start: int, end: int, align: int) -> tuple[int, int] | None:
    astart = ((start + align - 1) // align) * align
    aend = (end // align) * align + (align - 1)
    if aend > end:
        aend -= align
    if astart > end or aend < start or astart > aend:
        return None
    return astart, aend


def sectors_to_mib(sectors: int) -> float:
    return sectors * SECTOR_SIZE / (1024 * 1024)


def pe_for_pv(sectors: int) -> str:
    size_mib = sectors_to_mib(sectors)
    return "4M" if size_mib >= BIG_PV_MIB else "1M"


def pick_vg_pe(pv_ranges: list[tuple[int, int]]) -> str:
    if not pv_ranges:
        return "4M"
    for s, e in pv_ranges:
        if sectors_to_mib(e - s + 1) < BIG_PV_MIB:
            return "1M"
    return "4M"


def partition_path(device: str, number: int) -> str:
    # nvme/mmc naming includes a 'p' before partition number.
    if device[-1].isdigit():
        return f"{device}p{number}"
    return f"{device}{number}"


def get_device_pttype(device: str) -> str:
    proc = run_cmd(tool_cmd("lsblk", "-dn", "-o", "PTTYPE", device), capture=True, echo=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to detect partition table type")
    return proc.stdout.strip().lower()


def get_device_total_sectors(device: str) -> int:
    proc = run_cmd(tool_cmd("lsblk", "-b", "-dn", "-o", "SIZE", device), capture=True, echo=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to read device size")
    size_bytes = int(proc.stdout.strip())
    return size_bytes // SECTOR_SIZE


def clip_ranges_to_bounds(ranges: list[tuple[int, int]], lower: int, upper: int) -> list[tuple[int, int]]:
    clipped: list[tuple[int, int]] = []
    for start, end in ranges:
        ns = max(start, lower)
        ne = min(end, upper)
        if ns <= ne:
            clipped.append((ns, ne))
    return clipped


def get_existing_partition_count(device: str) -> int:
    cmd = tool_cmd("lsblk", "-nrpo", "NAME,TYPE", device)
    proc = run_cmd(cmd, capture=True, echo=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to list block devices")
    count = 0
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "part":
            count += 1
    return count


def create_sfdisk_script(ranges: list[tuple[int, int]], pttype: str) -> str:
    lines: list[str] = []
    lines.append(f"label: {pttype}")
    if pttype == "gpt":
        ptype = "E6D6D379-F507-44C2-A23C-238F2A3DF928"  # Linux LVM on GPT
    else:
        ptype = "8e"  # Linux LVM on DOS/MBR
    for start, end in ranges:
        size = end - start + 1
        lines.append(f"start={start}, size={size}, type={ptype}")
    return "\n".join(lines) + "\n"


def apply_partitioning(
    device: str,
    ranges: list[tuple[int, int]],
    pttype: str,
    dry_run: bool,
    interactive: bool,
) -> None:
    script = create_sfdisk_script(ranges, pttype)
    print("\nsfdisk script to apply:")
    print(script, end="")

    if dry_run:
        return

    proc = run_cmd(
        tool_cmd("sfdisk", "--wipe", "always", "--wipe-partitions", "always", device),
        dry_run=False,
        interactive=interactive,
        input_text=script,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sfdisk failed with code {proc.returncode}")

    run_cmd(tool_cmd("partprobe", device), dry_run=False, interactive=interactive)


def apply_pvcreate(
    device: str,
    start_part_num: int,
    count: int,
    dry_run: bool,
    interactive: bool,
) -> list[str]:
    pv_paths: list[str] = []
    for idx in range(count):
        pnum = start_part_num + idx
        ppath = partition_path(device, pnum)
        pv_paths.append(ppath)
        cmd = tool_cmd("pvcreate", "-ff", "-y", ppath)
        rc = run_cmd(cmd, dry_run=dry_run, interactive=interactive).returncode
        if rc != 0:
            raise RuntimeError(f"pvcreate failed for {ppath}")
    return pv_paths


def apply_vgcreate(
    vg_name: str,
    pe_size: str,
    pv_paths: list[str],
    dry_run: bool,
    interactive: bool,
) -> None:
    cmd = tool_cmd("vgcreate", "-s", pe_size, vg_name) + pv_paths
    rc = run_cmd(cmd, dry_run=dry_run, interactive=interactive).returncode
    if rc != 0:
        raise RuntimeError(f"vgcreate failed for VG {vg_name}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="recover_disk.py",
        description="Build LVM PV partition plan from scanner JSONL",
    )
    p.add_argument("status_file", help="Path to sector_status_disk.jsonl")
    p.add_argument("--device", required=True, help="Target disk device (e.g. /dev/sde)")
    p.add_argument(
        "--guard-mib",
        type=int,
        default=DEFAULT_GUARD_MIB,
        help="Guard margin around non-PASS sectors in MiB (default: 1)",
    )
    p.add_argument(
        "--min-pv-mib",
        type=int,
        default=MIN_PV_MIB,
        help="Minimum partition size in MiB (default: 8)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply sfdisk/pvcreate actions (default is plan-only)",
    )
    p.add_argument(
        "--create-vg",
        action="store_true",
        help="Create a VG from all newly created PVs",
    )
    p.add_argument("--vg-name", default="recdiskvg", help="VG name when --create-vg is used")
    p.add_argument(
        "--no-interactive",
        action="store_true",
        help="Disable per-command confirmations in --apply mode",
    )
    args = p.parse_args()
    if args.min_pv_mib < 8:
        p.error("--min-pv-mib must be >= 8")
    if args.guard_mib < 0:
        p.error("--guard-mib must be >= 0")
    if args.create_vg and not args.vg_name:
        p.error("--vg-name is required when --create-vg is used")
    return args


def main() -> int:
    args = parse_args()
    interactive_apply = args.apply and (not args.no_interactive)

    present_tools, missing_tools, _ = collect_tool_status(include_vgcreate=args.create_vg)
    required_for_plan = {"lsblk", "sfdisk"}
    required_for_apply = required_for_plan | {"partprobe", "pvcreate"}
    if args.create_vg:
        required_for_apply.add("vgcreate")

    missing_required_plan = sorted(required_for_plan.intersection(missing_tools))
    if missing_required_plan:
        print("Missing required tools for plan: " + ", ".join(missing_required_plan), file=sys.stderr)
        return 2

    status_path = Path(args.status_file)
    if not status_path.exists():
        print(f"Status file not found: {status_path}", file=sys.stderr)
        return 1

    segments = load_segments_jsonl(status_path)

    cover_start = min(b for b, _, _ in segments)
    cover_end = max(b + s - 1 for b, s, _ in segments)

    guard_sectors = args.guard_mib * MIB_SECTORS
    min_pv_sectors = args.min_pv_mib * MIB_SECTORS

    target_label = "gpt"
    try:
        current_pttype = get_device_pttype(args.device)
        total_sectors = get_device_total_sectors(args.device)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    blocked: list[tuple[int, int]] = []
    for begin, sectors, st in segments:
        if st == STATUS_PASS:
            continue
        s = begin
        e = begin + sectors - 1
        blocked.append((max(cover_start, s - guard_sectors), min(cover_end, e + guard_sectors)))
    blocked = merge_ranges(blocked)

    candidates = invert_ranges(cover_start, cover_end, blocked)

    # Respect partition table reserved areas.
    if target_label == "gpt":
        gpt_first_usable = 34
        gpt_last_usable = total_sectors - 34
        candidates = clip_ranges_to_bounds(candidates, gpt_first_usable, gpt_last_usable)

    usable: list[tuple[int, int]] = []
    dropped_small = 0
    for s, e in candidates:
        aligned = align_range(s, e, MIB_SECTORS)
        if not aligned:
            dropped_small += 1
            continue
        astart, aend = aligned
        if aend - astart + 1 < min_pv_sectors:
            dropped_small += 1
            continue
        usable.append((astart, aend))

    print("=" * 72)
    print("recover_disk plan")
    print("=" * 72)
    print("Plan mode uses read-only, non-privileged commands.")
    print("\n[Disk Status]")
    print(f"Status file        : {status_path}")
    print(f"Target device      : {args.device}")
    print(f"Current table      : {current_pttype or 'unknown'}")
    print(f"Planned table      : {target_label}")
    print(f"Covered range      : LBA {cover_start}..{cover_end}")
    print(f"Guard margin       : {args.guard_mib} MiB")
    print(f"Minimum PV size    : {args.min_pv_mib} MiB")
    print(f"Excluded ranges    : {len(blocked)}")
    print(f"Usable PV ranges   : {len(usable)}")
    print(f"Dropped tiny ranges: {dropped_small}")

    if not usable:
        print("\nNo usable ranges found for PV creation.")
        return 0

    print("\n[Planned Partitions]")
    for i, (s, e) in enumerate(usable, start=1):
        sectors = e - s + 1
        size_mib = sectors_to_mib(sectors)
        print(
            f"  PV#{i:02d} LBA {s}..{e} | sectors={sectors} | size={size_mib:.1f} MiB | PE-policy={pe_for_pv(sectors)}"
        )

    vg_pe = pick_vg_pe(usable)
    print(f"\nSuggested VG PE size: {vg_pe}")

    try:
        existing_parts = get_existing_partition_count(args.device)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Existing partitions: {existing_parts}")

    print("\n[Utilities]")
    for tool in sorted({name for name, _ in present_tools} | set(missing_tools)):
        path = TOOL_PATHS.get(tool, tool)
        if tool in missing_tools:
            print(f"  MISSING {tool}")
        else:
            print(f"  PRESENT {tool}: {path}")

    sfdisk_script = create_sfdisk_script(usable, target_label)
    print("\n[Commands To Execute]")
    print(f"$ sfdisk --wipe always --wipe-partitions always {args.device} <<'EOF'")
    print(sfdisk_script, end="")
    print("EOF")
    for idx in range(len(usable)):
        print(f"$ {TOOL_PATHS.get('pvcreate', 'pvcreate')} -ff -y {partition_path(args.device, existing_parts + 1 + idx)}")
    if args.create_vg:
        pv_paths = [partition_path(args.device, existing_parts + 1 + i) for i in range(len(usable))]
        print(f"$ {TOOL_PATHS.get('vgcreate', 'vgcreate')} -s {vg_pe} {args.vg_name} {' '.join(pv_paths)}")

    if args.apply:
        missing_required_apply = sorted(required_for_apply.intersection(missing_tools))
        if missing_required_apply:
            print(
                "\nMissing required tools for apply: " + ", ".join(missing_required_apply),
                file=sys.stderr,
            )
            return 2

    if existing_parts > 0:
        print(
            "\nRefusing to continue: disk already contains partitions. "
            "This tool requires an empty disk.",
            file=sys.stderr,
        )
        return 3

    if not args.apply:
        vg_name = args.vg_name if args.create_vg else "<vg_name>"
        print("\nPlan-only mode (no changes applied). Use --apply to execute.")
        print("\nAfter VG creation, manual LV + ext4 example (not executed by script):")
        if not args.create_vg:
            pv_paths = [partition_path(args.device, existing_parts + 1 + i) for i in range(len(usable))]
            print(f"  vgcreate -s {vg_pe} {vg_name} {' '.join(pv_paths)}")
        print(f"  lvcreate -n data -l 100%FREE {vg_name}")
        print(f"  mkfs.ext4 /dev/{vg_name}/data")
        print(f"  mkdir -p /mnt/{vg_name}")
        print(f"  mount /dev/{vg_name}/data /mnt/{vg_name}")
        return 0

    try:
        apply_partitioning(
            args.device,
            usable,
            target_label,
            dry_run=False,
            interactive=interactive_apply,
        )
        pv_paths = apply_pvcreate(
            args.device,
            existing_parts + 1,
            len(usable),
            dry_run=False,
            interactive=interactive_apply,
        )
        if args.create_vg:
            apply_vgcreate(
                args.vg_name,
                vg_pe,
                pv_paths,
                dry_run=False,
                interactive=interactive_apply,
            )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4

    print("\nDone.")
    print("Created PVs:")
    for p in pv_paths:
        print(f"  {p}")

    vg_name = args.vg_name if args.create_vg else "<vg_name>"
    if args.create_vg:
        print(f"Created VG: {vg_name} (PE={vg_pe})")
    else:
        print("VG was not created. Use --create-vg to do it automatically.")
        print("Manual VG creation command:")
        print(f"  vgcreate -s {vg_pe} {vg_name} {' '.join(pv_paths)}")

    print("\nNext manual steps (not executed by script):")
    print(f"  lvcreate -n data -l 100%FREE {vg_name}")
    print(f"  mkfs.ext4 /dev/{vg_name}/data")
    print(f"  mkdir -p /mnt/{vg_name}")
    print(f"  mount /dev/{vg_name}/data /mnt/{vg_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
