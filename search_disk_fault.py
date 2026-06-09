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

Scan disk for faults using dd tools and create a structured
JSONL output to list the various segments and their statuses.

"""

import argparse
import ast
import json
import operator
import os
import stat
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

UNKNOWN = 0
PASS = 1
FAIL = 2
STATUS_TO_NAME = {UNKNOWN: "unknown", PASS: "pass", FAIL: "fail"}
NAME_TO_STATUS = {"unknown": UNKNOWN, "pass": PASS, "fail": FAIL}
CHAR_TO_STATUS = {"U": UNKNOWN, "P": PASS, "F": FAIL}
STATUS_TO_CHAR = {UNKNOWN: "U", PASS: "P", FAIL: "F"}

ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


@dataclass(frozen=True)
class Segment:
    begin: int
    sectors: int
    status: int
    updated_at: str

    @property
    def end(self) -> int:
        return self.begin + self.sectors - 1


def latest_updated_at(ts_a: str, ts_b: str) -> str:
    """Return the most recent ISO timestamp; fallback to lexical compare."""
    try:
        return ts_a if datetime.fromisoformat(ts_a) >= datetime.fromisoformat(ts_b) else ts_b
    except Exception:
        return ts_a if ts_a >= ts_b else ts_b


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_block_device(path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except FileNotFoundError:
        return False


def get_device_start_lba(device: str, base_lba_arg: int | None) -> int:
    if base_lba_arg is not None:
        return base_lba_arg
    try:
        out = subprocess.check_output(["lsblk", "-dn", "-o", "START", device], text=True).strip()
    except Exception:
        return 0
    return int(out) if out else 0


def get_parent_disk_device(device: str) -> str:
    try:
        out = subprocess.check_output(["lsblk", "-dn", "-o", "PKNAME", device], text=True).strip()
    except Exception:
        return device
    if not out:
        return device
    return out if out.startswith("/dev/") else f"/dev/{out}"


def get_device_size_sectors(device: str) -> int:
    try:
        out = subprocess.check_output(["blockdev", "--getsz", device], text=True).strip()
    except Exception:
        return 0
    return int(out) if out else 0


def get_device_readahead_sectors(device: str) -> int:
    """Return kernel read-ahead in sectors (same unit as blockdev --getra)."""
    try:
        out = subprocess.check_output(["blockdev", "--getra", device], text=True).strip()
        if out:
            return int(out)
    except Exception:
        pass

    # Fallback to sysfs read_ahead_kb (KiB), convert to 512-byte sectors.
    dev_name = os.path.basename(device)
    sysfs_path = Path(f"/sys/class/block/{dev_name}/queue/read_ahead_kb")
    try:
        kb = int(sysfs_path.read_text(encoding="utf-8").strip())
        if kb > 0:
            return kb * 2
    except Exception:
        pass

    return 0


def build_macros(device: str, base_lba_arg: int | None) -> dict[str, int]:
    partition_start = get_device_start_lba(device, base_lba_arg)
    partition_size = get_device_size_sectors(device)
    disk_device = get_parent_disk_device(device)
    disk_size = get_device_size_sectors(disk_device)
    return {
        "disk_start": 0,
        "partition_start": partition_start,
        "disk_size": disk_size,
        "partition_size": partition_size,
    }


def safe_eval_int(expr: str, macros: dict[str, int]) -> int:
    tree = ast.parse(expr, mode="eval")

    def eval_node(node: ast.AST) -> int:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return int(node.value)
        if isinstance(node, ast.Name):
            if node.id not in macros:
                raise ValueError(f"Unknown macro: {node.id}")
            return int(macros[node.id])
        if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_UNARYOPS:
            return int(ALLOWED_UNARYOPS[type(node.op)](eval_node(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BINOPS:
            left = eval_node(node.left)
            right = eval_node(node.right)
            return int(ALLOWED_BINOPS[type(node.op)](left, right))
        raise ValueError(f"Unsupported expression: {expr}")

    return eval_node(tree.body)


def resolve_target_value(value: str, macros: dict[str, int]) -> int:
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        return safe_eval_int(text, macros)



def map_lba(begin_lba: int, lba_mode: str, device_start: int) -> int:
    if lba_mode == "device":
        return begin_lba
    if device_start > 0:
        mapped = begin_lba - device_start
        if mapped < 0:
            raise ValueError(f"mapped LBA is negative: begin={begin_lba}, start={device_start}")
        return mapped
    return begin_lba


def run_dd_read(
    device: str,
    skip_lba: int,
    count: int,
    timeout_sec: int,
    log_fh,
    dd_progress: bool,
    dd_bs_bytes: int,
    scan_sector_count: int,
) -> bool:
    transfer_bytes = count * 512
    skip_bytes = skip_lba * 512

    if dd_bs_bytes > 0:
        bs_bytes = dd_bs_bytes
    else:
        # Heuristic: tune bs from scan chunk size, cap to avoid huge allocations.
        bs_bytes = min(max(scan_sector_count * 512, 64 * 1024), 8 * 1024 * 1024)
        bs_bytes = min(bs_bytes, max(512, transfer_bytes))

    cmd = [
        "dd",
        f"if={device}",
        "of=/dev/null",
        f"bs={bs_bytes}",
        "iflag=skip_bytes,count_bytes",
        f"skip={skip_bytes}",
        f"count={transfer_bytes}",
        "status=progress" if dd_progress else "status=none",
    ]
    log_fh.write(
        f"DD CMD bs={bs_bytes} skip_lba={skip_lba} count_sectors={count} timeout={timeout_sec}s progress={dd_progress}\n"
    )
    log_fh.flush()
    try:
        if dd_progress:
            # Route dd progress to this program stdout for visual feedback.
            proc = subprocess.run(
                cmd,
                stdout=None,
                stderr=None,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        else:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
    except subprocess.TimeoutExpired:
        log_fh.write(
            f"DD TIMEOUT skip_lba={skip_lba} count_sectors={count} skip_bytes={skip_bytes} count_bytes={transfer_bytes} bs={bs_bytes}\n"
        )
        log_fh.flush()
        return False

    if proc.returncode == 0:
        return True
    if not dd_progress and proc.stderr:
        log_fh.write(proc.stderr)
    log_fh.write(
        f"DD FAIL rc={proc.returncode} skip_lba={skip_lba} count_sectors={count} skip_bytes={skip_bytes} count_bytes={transfer_bytes} bs={bs_bytes}\n"
    )
    log_fh.flush()
    return False


class StatusStore:
    """Compact JSONL status store with contiguous same-status segments.

    File format: one JSON object per line, for example:
      {"begin":0,"sectors":1048576,"updated_at":"...","globstatus":"P"}
      {"begin":1048576,"sectors":32,"updated_at":"...","globstatus":"U"}

        Legacy per-sector status strings are not supported in this scanner.
    """

    def __init__(self, status_file: Path, begin: int, sectors: int):
        self.status_file = status_file
        self.begin = begin
        self.sectors = sectors
        self.end = begin + sectors - 1
        self.all_segments: list[Segment] = []
        self.segments: list[Segment] = []
        self._starts: list[int] = []
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self.status_file.exists():
            raw = self.status_file.read_text(encoding="utf-8").strip()
            if not raw:
                raise RuntimeError(f"Empty status file: {self.status_file}")
            self.all_segments = self._load_segments(raw)
            self.all_segments = self._normalize_all_segments(self.all_segments)
            self._refresh_window()
            return

        self.all_segments = [Segment(self.begin, self.sectors, UNKNOWN, now_iso())]
        self._refresh_window()
        self.flush()

    def _normalize_all_segments(self, segments: list[Segment]) -> list[Segment]:
        segments = sorted(segments, key=lambda s: s.begin)
        if not segments:
            raise RuntimeError("Status file contains no segments")

        # Merge adjacent segments with same status.
        # The merged segment keeps the most recent timestamp (right side segment).
        # Gaps are allowed; they will be filled by _refresh_window().
        merged: list[Segment] = [segments[0]]
        for seg in segments[1:]:
            prev = merged[-1]
            if prev.status == seg.status and prev.end + 1 == seg.begin:
                merged[-1] = Segment(
                    prev.begin,
                    prev.sectors + seg.sectors,
                    prev.status,
                    latest_updated_at(prev.updated_at, seg.updated_at),
                )
            else:
                merged.append(seg)
        return merged

    def _slice_segments(self, from_lba: int, to_lba: int) -> list[Segment]:
        sliced: list[Segment] = []
        for seg in self.all_segments:
            if seg.end < from_lba:
                continue
            if seg.begin > to_lba:
                break
            overlap_begin = max(seg.begin, from_lba)
            overlap_end = min(seg.end, to_lba)
            sliced.append(
                Segment(
                    overlap_begin,
                    overlap_end - overlap_begin + 1,
                    seg.status,
                    seg.updated_at,
                )
            )
        return sliced

    def _refresh_window(self) -> None:
        self.segments = self._slice_segments(self.begin, self.end)
        if not self.segments:
            # No coverage at all; create full UNKNOWN range
            self.segments = [Segment(self.begin, self.end - self.begin + 1, UNKNOWN, now_iso())]
        else:
            # Fill gaps with UNKNOWN segments
            filled: list[Segment] = []
            current_lba = self.begin
            for seg in self.segments:
                if seg.begin > current_lba:
                    gap_size = seg.begin - current_lba
                    filled.append(Segment(current_lba, gap_size, UNKNOWN, now_iso()))
                filled.append(seg)
                current_lba = seg.end + 1
            if current_lba <= self.end:
                gap_size = self.end - current_lba + 1
                filled.append(Segment(current_lba, gap_size, UNKNOWN, now_iso()))
            self.segments = filled
        self._refresh_starts()

    def _refresh_starts(self) -> None:
        self._starts = [seg.begin for seg in self.segments]

    def _load_segments(self, raw: str) -> list[Segment]:
        # JSONL or single JSON object
        if raw.startswith("{") and raw.count("\n") == 0:
            doc = json.loads(raw)
            return self._segments_from_doc(doc)

        # JSONL
        segments: list[Segment] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            segments.extend(self._segments_from_doc(doc))
        return segments

    def _segments_from_doc(self, doc: dict) -> list[Segment]:
        if "begin" not in doc or "sectors" not in doc:
            raise RuntimeError("Invalid status doc: missing begin/sectors")

        seg_begin = int(doc["begin"])
        seg_sectors = int(doc["sectors"])
        seg_updated_at = str(doc.get("updated_at", now_iso()))

        if "globstatus" in doc:
            ch = str(doc["globstatus"]).strip()[0]
            return [Segment(seg_begin, seg_sectors, CHAR_TO_STATUS[ch], seg_updated_at)]

        if "status" in doc:
            status = doc["status"]
            if isinstance(status, int):
                return [Segment(seg_begin, seg_sectors, status, seg_updated_at)]
            status = str(status)
            if len(status) == 1:
                return [Segment(seg_begin, seg_sectors, CHAR_TO_STATUS[status], seg_updated_at)]
            raise RuntimeError(
                "Unsupported JSONL status format: multi-char 'status' strings are not supported. "
                "Use one segment per line with 'globstatus'."
            )

        if "statuses" in doc:
            raise RuntimeError(
                "Unsupported legacy format: 'statuses' strings are not supported. "
                "Use JSONL segments with 'globstatus'."
            )

        raise RuntimeError("Invalid status doc: missing globstatus/status")

    def flush(self) -> None:
        tmp_path = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for seg in self.all_segments:
                payload = {
                    "begin": seg.begin,
                    "sectors": seg.sectors,
                    "updated_at": seg.updated_at,
                    "globstatus": STATUS_TO_CHAR[seg.status],
                }
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        os.replace(tmp_path, self.status_file)

    def get(self, lba: int) -> int:
        idx = bisect_right(self._starts, lba) - 1
        if idx < 0:
            raise IndexError(f"LBA out of status range: {lba}")
        seg = self.segments[idx]
        if lba > seg.end:
            raise IndexError(f"LBA out of status range: {lba}")
        return seg.status

    def set_range(self, from_lba: int, to_lba: int, status: int) -> None:
        if from_lba > to_lba:
            return
        if from_lba < self.begin or to_lba >= self.begin + self.sectors:
            raise IndexError(f"LBA range out of status bounds: {from_lba}..{to_lba}")

        change_time = now_iso()
        new_segments: list[Segment] = []
        inserted = False
        for seg in self.all_segments:
            if seg.end < from_lba or seg.begin > to_lba:
                new_segments.append(seg)
                continue
            if seg.begin < from_lba:
                new_segments.append(Segment(seg.begin, from_lba - seg.begin, seg.status, seg.updated_at))
            if not inserted:
                new_segments.append(Segment(from_lba, to_lba - from_lba + 1, status, change_time))
                inserted = True
            if seg.end > to_lba:
                new_segments.append(Segment(to_lba + 1, seg.end - to_lba, seg.status, seg.updated_at))

        if not inserted:
            # Range falls fully inside a gap of all_segments: create it directly.
            new_segments.append(Segment(from_lba, to_lba - from_lba + 1, status, change_time))

        self.all_segments = self._normalize_all_segments(self._merge_adjacent(new_segments))
        self._refresh_window()

    @staticmethod
    def _merge_adjacent(segments: list[Segment]) -> list[Segment]:
        if not segments:
            return []
        segments = sorted(segments, key=lambda s: s.begin)
        merged = [segments[0]]
        for seg in segments[1:]:
            prev = merged[-1]
            if prev.status == seg.status and prev.end + 1 == seg.begin:
                merged[-1] = Segment(
                    prev.begin,
                    prev.sectors + seg.sectors,
                    prev.status,
                    latest_updated_at(prev.updated_at, seg.updated_at),
                )
            else:
                merged.append(seg)
        return merged

    def chunk_has_status(self, from_lba: int, to_lba: int, status: int) -> bool:
        for seg in self._overlapping_segments(from_lba, to_lba):
            if seg.status == status:
                return True
        return False

    def chunk_needs_test(self, from_lba: int, to_lba: int, retest_fail: bool, retest_pass: bool) -> bool:
        for seg in self._overlapping_segments(from_lba, to_lba):
            if seg.status == UNKNOWN:
                return True
            if retest_fail and seg.status == FAIL:
                return True
            if retest_pass and seg.status == PASS:
                return True
        return False

    def chunk_original_statuses(self, from_lba: int, to_lba: int) -> bytes:
        result = bytearray()
        for seg in self._overlapping_segments(from_lba, to_lba):
            overlap_begin = max(from_lba, seg.begin)
            overlap_end = min(to_lba, seg.end)
            result.extend(bytes([seg.status]) * (overlap_end - overlap_begin + 1))
        return bytes(result)

    def _overlapping_segments(self, from_lba: int, to_lba: int) -> list[Segment]:
        if from_lba > to_lba:
            return []
        idx = bisect_right(self._starts, from_lba) - 1
        if idx < 0:
            idx = 0
        overlaps: list[Segment] = []
        while idx < len(self.segments):
            seg = self.segments[idx]
            if seg.begin > to_lba:
                break
            if seg.end >= from_lba:
                overlaps.append(seg)
            idx += 1
        return overlaps

    def counts(self) -> dict[str, int]:
        counts = {UNKNOWN: 0, PASS: 0, FAIL: 0}
        for seg in self.segments:
            counts[seg.status] += seg.sectors
        return {
            "unknown": counts[UNKNOWN],
            "pass": counts[PASS],
            "fail": counts[FAIL],
        }

    def export_text(self, out_path: Path) -> None:
        with out_path.open("w", encoding="utf-8") as fh:
            for seg in self.segments:
                fh.write(f"{seg.begin} {seg.sectors} {STATUS_TO_NAME[seg.status]}\n")

    def import_text(self, in_path: Path) -> None:
        segments: list[Segment] = []
        for line in in_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 1:
                # legacy one-status-per-line format
                if not segments:
                    segments = [Segment(self.begin, self.sectors, NAME_TO_STATUS[parts[0]], now_iso())]
                else:
                    raise RuntimeError("Mixed legacy text formats are not supported")
                continue
            if len(parts) != 3:
                raise RuntimeError(f"Invalid text status line: {line}")
            seg_begin = int(parts[0])
            seg_sectors = int(parts[1])
            seg_status = NAME_TO_STATUS[parts[2]]
            segments.append(Segment(seg_begin, seg_sectors, seg_status, now_iso()))

        if not segments:
            raise RuntimeError(f"Empty text status file: {in_path}")

        imported = self._normalize_all_segments(self._merge_adjacent(segments))

        # Replace only the current window in the global store.
        # Gaps will be auto-filled by _refresh_window() with UNKNOWN.
        replacement: list[Segment] = []
        for seg in self.all_segments:
            if seg.end < self.begin or seg.begin > self.end:
                replacement.append(seg)
        replacement.extend(imported)
        self.all_segments = self._normalize_all_segments(self._merge_adjacent(replacement))
        self._refresh_window()
        self.flush()


def scan_target_range(args) -> int:
    if not is_block_device(args.device):
        print(f"Device not found or not block device: {args.device}", file=sys.stderr)
        return 3

    macros = build_macros(args.device, args.base_lba)
    target_begin = resolve_target_value(args.target_begin, macros)
    if args.target_end:
        target_end = resolve_target_value(args.target_end, macros)
        if target_end < target_begin:
            print("target end must be >= target begin", file=sys.stderr)
            return 2
        target_sectors = target_end - target_begin + 1
    else:
        target_sectors = resolve_target_value(args.target_sectors, macros)
    if target_sectors < 1:
        print("target sectors must be >= 1", file=sys.stderr)
        return 2
    end = target_begin + target_sectors - 1

    device_start = get_device_start_lba(args.device, args.base_lba)
    status_store = StatusStore(Path(args.status_file), target_begin, target_sectors)

    if args.import_text_status:
        status_store.import_text(Path(args.import_text_status))

    with open(args.log_file, "a", encoding="utf-8") as log:
        log.write(f"\n=== Start scan {now_iso()} ===\n")
        log.write(f"device={args.device} lba_mode={args.lba_mode} device_start={device_start}\n")
        log.write(
            f"macros disk_start={macros['disk_start']} partition_start={macros['partition_start']} disk_size={macros['disk_size']} partition_size={macros['partition_size']}\n"
        )
        log.write(
            f"target={target_begin}..{end} sectors={target_sectors} chunk={args.scan_sector_count} retries={args.retries}\n"
        )
        log.write(f"retest_fail={args.retest_fail} retest_pass={args.retest_pass}\n")
        log.write(
            f"dd_progress={args.dd_progress} dd_bs_bytes={args.dd_bs_bytes if args.dd_bs_bytes > 0 else 'auto'}\n"
        )
        log.flush()

        changed_statuses: list[tuple[int, str, str]] = []
        track_changes = args.retest_fail or args.retest_pass

        # Default scan targets unknown sectors.
        # Retest modes are explicit: when enabled, only requested statuses are retested.
        target_statuses = set()
        if args.retest_fail:
            target_statuses.add(FAIL)
        if args.retest_pass:
            target_statuses.add(PASS)
        if not target_statuses:
            target_statuses.add(UNKNOWN)

        # Optimization: iterate directly over matching status segments, skipping
        # all other covered ranges entirely (no per-sector scan loops).
        candidate_ranges: list[tuple[int, int]] = []
        for seg in status_store.segments:
            if seg.status in target_statuses:
                candidate_ranges.append((seg.begin, seg.end))

        if not candidate_ranges:
            log.write("No matching ranges for selected retest mode; exiting quickly.\n")
            log.flush()
        else:
            for range_start, range_end in candidate_ranges:
                test_from = range_start
                while test_from <= range_end:
                    test_to = min(test_from + args.scan_sector_count - 1, range_end)
                    original_chunk = status_store.chunk_original_statuses(test_from, test_to)

                    try:
                        mapped = map_lba(test_from, args.lba_mode, device_start)
                    except ValueError as e:
                        print(str(e), file=sys.stderr)
                        return 5

                    # Ensure the chunk being tested is marked as FAIL before running the
                    # actual dd test. This prevents adjacent ranges from being merged
                    # prematurely while the test is in-flight (regression observed when
                    # using --retest-pass/--retest-fail). Always mark prior to testing.
                    status_store.set_range(test_from, test_to, FAIL)
                    status_store.flush()

                    test_count = test_to - test_from + 1
                    log.write(f"TEST chunk lba={test_from}..{test_to} mapped={mapped} count={test_count}\n")
                    log.flush()

                    ok = True
                    for attempt in range(1, args.retries + 1):
                        if run_dd_read(
                            args.device,
                            mapped,
                            test_count,
                            args.timeout_sec,
                            log,
                            args.dd_progress,
                            args.dd_bs_bytes,
                            args.scan_sector_count,
                        ):
                            continue
                        log.write(f"READ FAIL attempt={attempt} lba={test_from}..{test_to}\n")
                        log.flush()
                        ok = False
                        break

                    if ok:
                        status_store.set_range(test_from, test_to, PASS)
                        status_store.flush()
                        log.write(f"PASS chunk lba={test_from}..{test_to}\n")
                    else:
                        status_store.set_range(test_from, test_to, FAIL)
                        status_store.flush()
                        log.write(f"FAIL chunk lba={test_from}..{test_to}\n")

                    final_chunk = status_store.chunk_original_statuses(test_from, test_to)
                    if track_changes and original_chunk != final_chunk:
                        for i, old_b in enumerate(original_chunk):
                            new_b = final_chunk[i]
                            if old_b != new_b:
                                sector = test_from + i
                                changed_statuses.append(
                                    (
                                        sector,
                                        STATUS_TO_NAME.get(old_b, "unknown"),
                                        STATUS_TO_NAME.get(new_b, "unknown"),
                                    )
                                )

                    log.flush()

                    if not is_block_device(args.device):
                        log.write(f"DEVICE DISAPPEARED at lba={test_from}\n")
                        log.flush()
                        return 4

                    test_from = test_to + 1

        counts = status_store.counts()
        if changed_statuses:
            log.write("STATUS CHANGES DURING RETEST (sector old->new):\n")
            for sector, old_s, new_s in changed_statuses:
                log.write(f"{sector} {old_s}->{new_s}\n")
        else:
            log.write("STATUS CHANGES DURING RETEST: none\n")
        log.write(
            f"DONE {now_iso()} unknown={counts['unknown']} pass={counts['pass']} fail={counts['fail']}\n"
        )
        log.flush()

    if args.export_text_status:
        status_store.export_text(Path(args.export_text_status))

    counts = status_store.counts()
    print(
        "Scan complete "
        f"unknown={counts['unknown']} pass={counts['pass']} fail={counts['fail']}"
    )
    return 0


def parse_args():
    p = argparse.ArgumentParser(
        prog="search_disk_fault.py",
        description="Fast persistent LBA scanner with compressed JSONL status segments"
    )
    p.add_argument("device", help="Block device (e.g. /dev/sdf2)")

    p.add_argument("--lba-mode", choices=["absolute", "device"], default="absolute")
    p.add_argument("--base-lba", type=int, default=None)

    p.add_argument(
        "--target-begin",
        type=str,
        required=True,
        help="Start LBA or macro/expression: disk_start, partition_start, partition_start+2048",
    )
    p.add_argument(
        "--target-sectors",
        type=str,
        default="",
        help="Sector count or macro/expression: disk_size, partition_size",
    )
    p.add_argument(
        "--target-end",
        type=str,
        default="",
        help="End LBA (inclusive) or macro/expression. Alternative to --target-sectors",
    )

    p.add_argument("--scan-sector-count", type=int, default=1)
    p.add_argument(
        "--dd-progress",
        action="store_true",
        help="Show dd status=progress output in program stdout",
    )
    p.add_argument(
        "--dd-bs-bytes",
        type=int,
        default=0,
        help="dd block size in bytes (0=auto from scan-sector-count, capped)",
    )
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--retest-fail", action="store_true")
    p.add_argument("--retest-pass", action="store_true")

    p.add_argument("--status-file", default="sector_status.jsonl")
    p.add_argument(
        "--import-text-status",
        default="",
        help="Import legacy text status file (# begin/sectors/status) before scanning",
    )
    p.add_argument("--export-text-status", default="", help="Optional export in text format")
    p.add_argument("--log-file", default="test_ranges_python.log")
    p.add_argument("--timeout-sec", type=int, default=30)

    args = p.parse_args()

    if not args.target_sectors and not args.target_end:
        p.error("one of --target-sectors or --target-end is required")
    if args.target_sectors and args.target_end:
        p.error("--target-sectors and --target-end are mutually exclusive")

    if args.scan_sector_count < 1:
        p.error("--scan-sector-count must be >= 1")
    if args.dd_bs_bytes < 0:
        p.error("--dd-bs-bytes must be >= 0")
    if args.dd_bs_bytes not in (0,) and args.dd_bs_bytes < 512:
        p.error("--dd-bs-bytes must be 0 or >= 512")
    if args.dd_bs_bytes not in (0,) and args.dd_bs_bytes % 512 != 0:
        p.error("--dd-bs-bytes must be a multiple of 512")
    if args.retries < 1:
        p.error("--retries must be >= 1")
    if args.timeout_sec < 1:
        p.error("--timeout-sec must be >= 1")

    # Sanity guard: scan chunks must be large enough to cover read-ahead side
    # effects from the previous block. Otherwise a fault can surface while
    # testing a nominally different chunk.
    readahead = get_device_readahead_sectors(args.device)
    min_scan_sector_count = readahead * 2
    if readahead > 0 and args.scan_sector_count < min_scan_sector_count:
        p.error(
            f"--scan-sector-count must be >= {min_scan_sector_count} "
            f"(2x readahead={readahead} sectors from disk {args.device})"
        )

    return args


def main() -> int:
    args = parse_args()
    return scan_target_range(args)


if __name__ == "__main__":
    raise SystemExit(main())
