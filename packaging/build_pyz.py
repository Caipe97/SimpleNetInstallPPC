#!/usr/bin/env python3
"""Build dist/simplenetinstallppc.pyz: the whole program in one file.

    python3 packaging/build_pyz.py [--output PATH]

The program uses only Python's standard library, so this one file runs on any
Linux or Mac with Python 3.9 or later, from wherever it's copied (a USB
stick, a home folder):

    sudo ./simplenetinstallppc.pyz serve
    sudo python3 simplenetinstallppc.pyz serve

Nothing is installed and no network is needed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "simplenetinstallppc"
INTERPRETER = "/usr/bin/env python3"

# zipapp's own generated __main__ ignores main()'s return value; this keeps
# the exit status (check and serve report failures through it).
MAIN = "from simplenetinstallppc.cli import main\nraise SystemExit(main())\n"


def build(output: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        shutil.copytree(PACKAGE, staging / PACKAGE.name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
        (staging / "__main__.py").write_text(MAIN)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target, then swapped in: a server running from the
        # old file keeps reading it, which overwriting in place would break.
        partial = output.with_name(output.name + ".partial")
        zipapp.create_archive(staging, partial, interpreter=INTERPRETER, compressed=True)
        partial.chmod(0o755)
        os.replace(partial, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "simplenetinstallppc.pyz")
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        print("needs Python 3.9 or later", file=sys.stderr)
        return 1
    path = build(args.output)
    print(f"built {path} ({path.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
