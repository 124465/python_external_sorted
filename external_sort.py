from __future__ import annotations

import argparse
import heapq
import io
import os
import shutil
import sys
import tempfile
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional


IntMode = Literal["signed", "unsigned"]
InputKind = Literal["binary", "text"]
OutputKind = Literal["binary", "text"]


def _detect_input_kind(path: Path) -> InputKind:
    if path.suffix.lower() in {".txt", ".csv", ".log"}:
        return "text"
    with path.open("rb") as f:
        sample = f.read(4096)
    if not sample:
        return "binary"
    allowed = b"0123456789+- \t\r\n"
    if all(b in allowed for b in sample):
        return "text"
    return "binary"


def _clamp_workers(workers: Optional[int]) -> int:
    cpu = os.cpu_count() or 1
    if workers is None:
        return cpu
    return max(1, min(cpu, workers))


def _choose_mode_from_text_sample(path: Path, sample_lines: int = 5000) -> IntMode:
    min_v: Optional[int] = None
    max_v: Optional[int] = None
    with path.open("rt", encoding="utf-8", errors="replace", newline="") as f:
        for _ in range(sample_lines):
            line = f.readline()
            if not line:
                break
            s = line.strip()
            if not s:
                continue
            v = int(s, 10)
            min_v = v if min_v is None else min(min_v, v)
            max_v = v if max_v is None else max(max_v, v)
    if min_v is not None and min_v < 0:
        return "signed"
    if max_v is not None and max_v > 2**31 - 1:
        return "unsigned"
    return "signed"


def _array_type(mode: IntMode) -> str:
    return "i" if mode == "signed" else "I"


def _check_32bit_array_type(typecode: str) -> None:
    if array(typecode).itemsize != 4:
        raise RuntimeError("This Python build does not support 32-bit 'i'/'I' arrays.")


def _sorted_run_path(tmp_dir: Path, run_id: int) -> Path:
    return tmp_dir / f"run_{run_id:09d}.bin"


def _merge_run_path(tmp_dir: Path, pass_id: int, group_id: int) -> Path:
    return tmp_dir / f"merge_p{pass_id:02d}_{group_id:09d}.bin"


def _read_ints_text_stream(f: io.TextIOBase, count: int) -> list[int]:
    out: list[int] = []
    while len(out) < count:
        line = f.readline()
        if not line:
            break
        s = line.strip()
        if not s:
            continue
        out.append(int(s, 10))
    return out


def _sort_and_write_run_from_numbers(numbers: list[int], run_path: str, mode: IntMode) -> str:
    numbers.sort()
    typecode = _array_type(mode)
    _check_32bit_array_type(typecode)
    a = array(typecode, numbers)
    with open(run_path, "wb") as out:
        a.tofile(out)
    return run_path


def _sort_and_write_run_from_binary_slice(
    input_path: str, start_index: int, count: int, run_path: str, mode: IntMode
) -> str:
    typecode = _array_type(mode)
    _check_32bit_array_type(typecode)
    with open(input_path, "rb") as f:
        f.seek(start_index * 4)
        a = array(typecode)
        a.fromfile(f, count)
    nums = a.tolist()
    nums.sort()
    out_arr = array(typecode, nums)
    with open(run_path, "wb") as out:
        out_arr.tofile(out)
    return run_path


@dataclass
class _RunReader:
    f: io.BufferedReader
    typecode: str
    buf_ints: int
    buf: array
    idx: int
    done: bool

    @classmethod
    def open(cls, path: Path, typecode: str, buf_ints: int) -> "_RunReader":
        _check_32bit_array_type(typecode)
        f = path.open("rb")
        return cls(f=f, typecode=typecode, buf_ints=max(1, buf_ints), buf=array(typecode), idx=0, done=False)

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass

    def _refill(self) -> None:
        if self.done:
            return
        self.buf = array(self.typecode)
        try:
            self.buf.fromfile(self.f, self.buf_ints)
        except EOFError:
            self.done = True
            self.idx = 0
            return
        if not self.buf:
            self.done = True
            self.idx = 0
            return
        self.idx = 0

    def pop(self) -> Optional[int]:
        if self.done:
            return None
        if self.idx >= len(self.buf):
            self._refill()
            if self.done:
                return None
        v = self.buf[self.idx]
        self.idx += 1
        return int(v)


def _merge_runs_to_binary(run_paths: list[Path], out_path: Path, mode: IntMode, max_in_mem_ints: int) -> None:
    typecode = _array_type(mode)
    streams_n = len(run_paths)
    if streams_n == 0:
        out_path.write_bytes(b"")
        return
    if max_in_mem_ints < max(1, streams_n + 1):
        buf_ints = 1
    else:
        buf_ints = max(1, (max_in_mem_ints - streams_n) // streams_n)

    readers: list[_RunReader] = [_RunReader.open(p, typecode=typecode, buf_ints=buf_ints) for p in run_paths]
    heap: list[tuple[int, int]] = []
    for i, r in enumerate(readers):
        v = r.pop()
        if v is not None:
            heap.append((v, i))
    heapq.heapify(heap)

    out_buf: array = array(typecode)
    out_buf_ints = max(1, min(max_in_mem_ints, 1 << 16))
    with out_path.open("wb") as out:
        while heap:
            v, i = heapq.heappop(heap)
            out_buf.append(v)
            if len(out_buf) >= out_buf_ints:
                out_buf.tofile(out)
                out_buf = array(typecode)
            nxt = readers[i].pop()
            if nxt is not None:
                heapq.heappush(heap, (nxt, i))
        if out_buf:
            out_buf.tofile(out)

    for r in readers:
        r.close()


def _merge_runs_to_text(run_paths: list[Path], out_path: Path, mode: IntMode, max_in_mem_ints: int) -> None:
    typecode = _array_type(mode)
    streams_n = len(run_paths)
    if streams_n == 0:
        out_path.write_text("", encoding="utf-8")
        return
    if max_in_mem_ints < max(1, streams_n + 1):
        buf_ints = 1
    else:
        buf_ints = max(1, (max_in_mem_ints - streams_n) // streams_n)

    readers: list[_RunReader] = [_RunReader.open(p, typecode=typecode, buf_ints=buf_ints) for p in run_paths]
    heap: list[tuple[int, int]] = []
    for i, r in enumerate(readers):
        v = r.pop()
        if v is not None:
            heap.append((v, i))
    heapq.heapify(heap)

    line_buf: list[str] = []
    line_buf_cap = max(1024, min(max_in_mem_ints, 1 << 16))
    with out_path.open("wt", encoding="utf-8", newline="\n") as out:
        while heap:
            v, i = heapq.heappop(heap)
            line_buf.append(str(v))
            if len(line_buf) >= line_buf_cap:
                out.write("\n".join(line_buf))
                out.write("\n")
                line_buf.clear()
            nxt = readers[i].pop()
            if nxt is not None:
                heapq.heappush(heap, (nxt, i))
        if line_buf:
            out.write("\n".join(line_buf))
            out.write("\n")

    for r in readers:
        r.close()


def _merge_all_runs(
    runs: list[Path],
    out_path: Path,
    out_kind: OutputKind,
    mode: IntMode,
    max_in_mem_ints: int,
    max_open_files: int = 64,
) -> None:
    if not runs:
        if out_kind == "binary":
            out_path.write_bytes(b"")
        else:
            out_path.write_text("", encoding="utf-8")
        return

    if len(runs) == 1:
        if out_kind == "binary":
            shutil.copyfile(runs[0], out_path)
        else:
            _merge_runs_to_text(runs, out_path, mode=mode, max_in_mem_ints=max_in_mem_ints)
        return

    tmp_dir = runs[0].parent
    pass_id = 0
    current = runs[:]
    while len(current) > 1:
        pass_id += 1
        next_runs: list[Path] = []
        group_id = 0
        for i in range(0, len(current), max_open_files):
            group = current[i : i + max_open_files]
            group_id += 1
            out_run = _merge_run_path(tmp_dir, pass_id, group_id)
            _merge_runs_to_binary(group, out_run, mode=mode, max_in_mem_ints=max_in_mem_ints)
            next_runs.append(out_run)
            for p in group:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        current = next_runs

    final_run = current[0]
    if out_kind == "binary":
        shutil.copyfile(final_run, out_path)
    else:
        _merge_runs_to_text([final_run], out_path, mode=mode, max_in_mem_ints=max_in_mem_ints)
    try:
        final_run.unlink()
    except FileNotFoundError:
        pass


def _create_sorted_runs(
    input_path: Path,
    kind: InputKind,
    mode: IntMode,
    max_in_mem_ints: int,
    workers: int,
    tmp_dir: Path,
) -> list[Path]:
    workers = _clamp_workers(workers)
    chunk_ints = max(1, max_in_mem_ints // workers)
    runs: list[Path] = []

    if kind == "binary":
        size = input_path.stat().st_size
        if size % 4 != 0:
            raise ValueError("Binary mode expects file size to be divisible by 4 bytes.")
        total_ints = size // 4
        tasks = []
        run_id = 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for start in range(0, total_ints, chunk_ints):
                run_id += 1
                count = min(chunk_ints, total_ints - start)
                run_path = _sorted_run_path(tmp_dir, run_id)
                tasks.append(
                    ex.submit(
                        _sort_and_write_run_from_binary_slice,
                        str(input_path),
                        int(start),
                        int(count),
                        str(run_path),
                        mode,
                    )
                )
            for fut in as_completed(tasks):
                run_s = fut.result()
                runs.append(Path(run_s))
    else:
        tasks = []
        run_id = 0
        with input_path.open("rt", encoding="utf-8", errors="replace", newline="") as f:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                while True:
                    chunk = _read_ints_text_stream(f, chunk_ints)
                    if not chunk:
                        break
                    run_id += 1
                    run_path = _sorted_run_path(tmp_dir, run_id)
                    tasks.append(ex.submit(_sort_and_write_run_from_numbers, chunk, str(run_path), mode))
                for fut in as_completed(tasks):
                    run_s = fut.result()
                    runs.append(Path(run_s))

    runs.sort()
    return runs


def _default_output_path(input_path: Path, kind: InputKind) -> Path:
    base = input_path.stem + "_sorted" + input_path.suffix
    if kind == "binary" and input_path.suffix.lower() in {".txt", ""}:
        base = input_path.stem + "_sorted.bin"
    return input_path.with_name(base)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("file", type=str)
    p.add_argument("max_in_memory_ints", type=int)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--output", type=str, default=None)
    p.add_argument("--signed", action="store_true")
    p.add_argument("--unsigned", action="store_true")
    p.add_argument("--max-open-files", type=int, default=64)

    args = p.parse_args(argv)

    input_path = Path(args.file).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(str(input_path))

    if args.max_in_memory_ints <= 0:
        raise ValueError("max_in_memory_ints must be > 0")

    kind: InputKind = _detect_input_kind(input_path)
    out_kind: OutputKind = "text" if kind == "text" else "binary"

    if args.signed and args.unsigned:
        raise ValueError("Choose at most one of --signed/--unsigned")
    if args.signed:
        mode: IntMode = "signed"
    elif args.unsigned:
        mode = "unsigned"
    else:
        mode = _choose_mode_from_text_sample(input_path) if kind == "text" else "signed"

    out_path = Path(args.output).expanduser().resolve() if args.output else _default_output_path(input_path, kind)
    if out_path.exists() and out_path.samefile(input_path):
        raise ValueError("Output path must be different from input path.")

    tmp_root = input_path.parent
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".runs_{input_path.stem}_", dir=str(tmp_root)))
    try:
        runs = _create_sorted_runs(
            input_path=input_path,
            kind=kind,
            mode=mode,
            max_in_mem_ints=args.max_in_memory_ints,
            workers=_clamp_workers(args.workers),
            tmp_dir=tmp_dir,
        )
        _merge_all_runs(
            runs=runs,
            out_path=out_path,
            out_kind=out_kind,
            mode=mode,
            max_in_mem_ints=args.max_in_memory_ints,
            max_open_files=max(2, int(args.max_open_files)),
        )
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
