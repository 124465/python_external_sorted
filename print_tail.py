from __future__ import annotations

import argparse
from typing import Optional

import print_head


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("sorted_file", type=str)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--output", type=str, default=None)
    p.add_argument("--signed", action="store_true")
    p.add_argument("--unsigned", action="store_true")
    args = p.parse_args(argv)

    forwarded: list[str] = [args.sorted_file, "--n", str(args.n), "--tail"]
    if args.output:
        forwarded.extend(["--output", args.output])
    if args.signed:
        forwarded.append("--signed")
    if args.unsigned:
        forwarded.append("--unsigned")
    return print_head.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
