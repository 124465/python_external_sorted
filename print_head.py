from __future__ import annotations

import argparse
import sys
from array import array
from collections import deque
from pathlib import Path
from typing import Literal, Optional, TextIO


IntMode = Literal["signed", "unsigned"]
InputKind = Literal["binary", "text"]


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


def _array_type(mode: IntMode) -> str:
    return "i" if mode == "signed" else "I"


def _check_32bit_array_type(typecode: str) -> None:
    if array(typecode).itemsize != 4:
        raise RuntimeError("This Python build does not support 32-bit 'i'/'I' arrays.")


def _read_head_text(path: Path, n: int) -> list[int]:
    out: list[int] = []
    with path.open("rt", encoding="utf-8", errors="replace", newline="") as f:
        while len(out) < n:
            line = f.readline()
            if not line:
                break
            s = line.strip()
            if not s:
                continue
            out.append(int(s, 10))
    return out


def _read_tail_text(path: Path, n: int) -> list[int]:
    buf: deque[int] = deque(maxlen=n)
    with path.open("rt", encoding="utf-8", errors="replace", newline="") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            buf.append(int(s, 10))
    return list(buf)


def _read_head_binary(path: Path, n: int, mode: IntMode) -> list[int]:
    typecode = _array_type(mode)
    _check_32bit_array_type(typecode)
    a = array(typecode)
    with path.open("rb") as f:
        try:
            a.fromfile(f, n)
        except EOFError:
            pass
    return [int(x) for x in a]


def _read_tail_binary(path: Path, n: int, mode: IntMode) -> list[int]:
    typecode = _array_type(mode)
    _check_32bit_array_type(typecode)
    size = path.stat().st_size
    if size % 4 != 0:
        raise ValueError("Binary mode expects file size to be divisible by 4 bytes.")
    total_ints = size // 4
    if total_ints == 0:
        return []

    start = max(0, total_ints - n)
    count = total_ints - start
    a = array(typecode)
    with path.open("rb") as f:
        f.seek(start * 4)
        try:
            a.fromfile(f, count)
        except EOFError:
            pass
    return [int(x) for x in a]


def _write_numbers(out: TextIO, nums: list[int]) -> None:
    for v in nums:
        out.write(str(v))
        out.write("\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("sorted_file", type=str)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--output", type=str, default=None)
    p.add_argument("--tail", action="store_true")
    p.add_argument("--signed", action="store_true")
    p.add_argument("--unsigned", action="store_true")
    args = p.parse_args(argv)

    path = Path(args.sorted_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    if args.n <= 0:
        raise ValueError("--n must be > 0")

    if args.signed and args.unsigned:
        raise ValueError("Choose at most one of --signed/--unsigned")
    mode: IntMode = "unsigned" if args.unsigned else "signed"

    kind = _detect_input_kind(path)
    if args.tail:
        nums = _read_tail_text(path, args.n) if kind == "text" else _read_tail_binary(path, args.n, mode=mode)
    else:
        nums = _read_head_text(path, args.n) if kind == "text" else _read_head_binary(path, args.n, mode=mode)

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        with out_path.open("wt", encoding="utf-8", newline="\n") as out:
            _write_numbers(out, nums)
    else:
        _write_numbers(sys.stdout, nums)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
