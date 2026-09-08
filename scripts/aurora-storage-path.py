#!/usr/bin/env python3
"""Translate corresponding GAMB2LE GWS and S3 paths."""

from __future__ import annotations

import argparse

GWS_PREFIX = "/gws/ssde/j25b/gamb2le/data/"
S3_PREFIX = "s3://gamb2le-o/data/"


def translate(value: str) -> str:
    if value.startswith(GWS_PREFIX):
        return S3_PREFIX + value[len(GWS_PREFIX):]
    if value.startswith(S3_PREFIX):
        return GWS_PREFIX + value[len(S3_PREFIX):]
    raise ValueError(
        f"path must begin with {GWS_PREFIX!r} or {S3_PREFIX!r}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    try:
        print(translate(args.path))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
