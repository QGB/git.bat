#!/usr/bin/env python3
"""Build and validate a self-contained script from the modular Dulwich tool."""

import argparse
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("shared.py", "lfs.py", "stage.py", "network.py", "push.py")
ORIGINAL = ROOT / "dulwich_push.py"


def _module_source(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)
    removed_lines = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            removed_lines.update(range(node.lineno, node.end_lineno + 1))

    return "".join(
        line for number, line in enumerate(lines, 1) if number not in removed_lines
    )


def assemble() -> str:
    return "".join(_module_source(ROOT / "dulwich_git" / module) for module in MODULES)


def validate_syntax_equivalence(source: str) -> None:
    generated_tree = ast.parse(source, filename="<generated dulwich_push.py>")
    original_tree = ast.parse(ORIGINAL.read_text(encoding="utf-8"), filename=str(ORIGINAL))
    if ast.dump(generated_tree, include_attributes=False) != ast.dump(
        original_tree, include_attributes=False
    ):
        raise ValueError(f"generated source differs syntactically from {ORIGINAL}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine the modular Dulwich tool into one standalone Python script."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "dulwich_push_merged.py",
        help="output path (default: dulwich_push_merged.py beside the original script)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="allow replacing an existing output file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that the output already matches without writing it",
    )
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    source = assemble()
    try:
        validate_syntax_equivalence(source)
    except (OSError, SyntaxError, ValueError) as error:
        parser.error(str(error))

    if args.check:
        if not output.is_file():
            parser.error(f"output file does not exist: {output}")
        if output.read_text(encoding="utf-8") != source:
            parser.error(f"generated file is out of date: {output}")
        print(f"Up to date; syntax matches {ORIGINAL}: {output}")
        return 0

    if output.exists() and not args.force:
        parser.error(f"output already exists; pass --force to replace it: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(source, encoding="utf-8", newline="\n")
    output.chmod(output.stat().st_mode | 0o111)
    print(f"Generated; syntax matches {ORIGINAL}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
