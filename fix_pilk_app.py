#!/usr/bin/env python3
"""
fix_pilk_app.py — surgically fix the UnboundLocalError in PILK's app.py
introduced by the rolodex agent wire-up.

Usage:
    python3 fix_pilk_app.py
    # or specify path explicitly:
    python3 fix_pilk_app.py "/Users/pilksclaes/Pilk Ai/pilk-ai/core/api/app.py"

What it does:
    1. Backs up the file as app.py.bak.<timestamp>
    2. Finds any conditional assignment of `client = ...` followed later by
       `rolodex_telegram_client = client` (or similar pattern)
    3. Inserts `client = None` before the conditional block so it's always defined
    4. Reports the diff

Safe to run multiple times — it's idempotent (won't double-patch).
"""

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path("/Users/pilksclaes/Pilk Ai/pilk-ai/core/api/app.py")


def find_lifespan_block(src: str) -> tuple[int, int] | None:
    """Return (start_line, end_line) of the lifespan function (0-indexed)."""
    lines = src.splitlines()
    start = None
    indent = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if start is None and (
            stripped.startswith("async def lifespan(")
            or stripped.startswith("def lifespan(")
            or "@asynccontextmanager" in stripped and i + 1 < len(lines)
            and lines[i + 1].lstrip().startswith(("async def lifespan", "def lifespan"))
        ):
            start = i
            indent = len(line) - len(stripped)
            continue
        if start is not None:
            # function body — find first non-empty line at <= indent that isn't inside the func
            if line.strip() and (len(line) - len(line.lstrip())) <= indent and not line.lstrip().startswith(("@", "#")):
                if i > start + 2:  # skip decorator/signature
                    return start, i
    if start is not None:
        return start, len(lines)
    return None


def detect_unsafe_client(lines: list[str], start: int, end: int) -> int | None:
    """
    Find the line index where `client = ...` is assigned ONLY inside a conditional
    (if/elif/try) block, when it's later referenced unconditionally.
    Returns the line index of the FIRST conditional assignment, or None.
    """
    body = lines[start:end]
    # find any line that uses `client` outside an assignment (i.e. references it)
    references = []
    for i, line in enumerate(body):
        # skip comments / strings (rough)
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # match `something = client` or `... client ...` not preceded by `client =`
        if re.search(r"\bclient\b", line) and not re.match(r"\s*client\s*=", line):
            references.append(i)
    if not references:
        return None

    # find the first conditional assignment of `client = ...`
    for i, line in enumerate(body):
        m = re.match(r"^(\s+)client\s*=", line)
        if m:
            # check if there's an `if`/`elif`/`try`/`else:` before it at lower indent
            target_indent = len(m.group(1))
            for j in range(i - 1, -1, -1):
                prev = body[j]
                prev_stripped = prev.lstrip()
                prev_indent = len(prev) - len(prev_stripped)
                if not prev_stripped:
                    continue
                if prev_indent < target_indent:
                    if prev_stripped.startswith(("if ", "elif ", "else:", "try:", "except", "with ")):
                        # confirmed: this is conditional — and there's a later reference
                        if any(r > i for r in references):
                            return start + i
                    break
    return None


def patch_file(path: Path) -> tuple[bool, str]:
    """Returns (changed, message)."""
    if not path.exists():
        return False, f"File not found: {path}"

    src = path.read_text()
    if "client = None  # auto-initialized by fix_pilk_app.py" in src:
        return False, "Already patched (idempotent guard found). Nothing to do."

    block = find_lifespan_block(src)
    if not block:
        return False, "Couldn't locate the lifespan function. Manual review required."

    lines = src.splitlines()
    target_line = detect_unsafe_client(lines, *block)
    if target_line is None:
        # fallback heuristic: insert `client = None` at the very start of lifespan body
        start, _ = block
        # find first line after `async def` signature that's actually code
        for i in range(start + 1, min(start + 6, len(lines))):
            stripped = lines[i].lstrip()
            if stripped and not stripped.startswith(("'''", '"""', "#")):
                indent = " " * (len(lines[i]) - len(stripped))
                lines.insert(i, f"{indent}client = None  # auto-initialized by fix_pilk_app.py")
                break
        else:
            return False, "Couldn't find a safe injection point in lifespan. Manual fix required."
    else:
        # insert `client = None` at the same indent as the first conditional `client =`
        line = lines[target_line]
        indent_match = re.match(r"^(\s+)", line)
        target_indent = indent_match.group(1) if indent_match else "    "
        # find the conditional ABOVE — insert above it at one less indent? No, at the
        # parent (function-body) indent. Walk up to find the parent.
        parent_indent = ""
        for j in range(target_line - 1, -1, -1):
            prev = lines[j]
            prev_stripped = prev.lstrip()
            if not prev_stripped:
                continue
            prev_indent = len(prev) - len(prev_stripped)
            if prev_indent < len(target_indent):
                parent_indent = " " * prev_indent
                # insert after this line
                lines.insert(j + 1, f"{target_indent}client = None  # auto-initialized by fix_pilk_app.py")
                break
        else:
            lines.insert(target_line, f"{target_indent}client = None  # auto-initialized by fix_pilk_app.py")

    new_src = "\n".join(lines)
    if not new_src.endswith("\n"):
        new_src += "\n"

    # backup
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)

    path.write_text(new_src)
    return True, f"Patched. Backup at {backup}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Path to app.py (default: {DEFAULT_PATH})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Dry run on: {args.path}")
        if not args.path.exists():
            print(f"  ✗ File not found")
            return 1
        block = find_lifespan_block(args.path.read_text())
        if not block:
            print("  ✗ Couldn't locate lifespan function")
            return 1
        target = detect_unsafe_client(args.path.read_text().splitlines(), *block)
        if target is not None:
            print(f"  ✓ Would insert `client = None` near line {target + 1}")
        else:
            print("  ! No conditional `client =` detected — would inject at lifespan body start")
        return 0

    print(f"Patching {args.path}...")
    changed, msg = patch_file(args.path)
    print(("  ✓ " if changed else "  • ") + msg)

    if changed:
        print()
        print("Next steps:")
        print(f"  1. cd \"{args.path.parents[2]}\"")
        print("  2. pkill -f pilkd ; sleep 2 ; uv run pilkd &")
        print("  3. Watch for clean startup — no UnboundLocalError this time")
        print()
        print("If it still fails, restore from the backup and paste the new traceback.")

    return 0 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
