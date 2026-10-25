#!/usr/bin/env python3
"""Build a self-contained script from the modular Git tool."""

import argparse
import ast
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "shared.py",
    "core.py",
    "lfs.py",
    "network.py",
    "identity.py",
    "commit.py",
    "history.py",
    "cli.py",
)
STAMP_FILES = MODULES + ("generate_single_file.py",)


def source_stamp() -> str:
    """Fingerprint of every source file that feeds assemble()."""
    digest = hashlib.sha256()
    for name in STAMP_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / "git" / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _module_source(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)
    removed_lines = set()

    for node in tree.body:
        is_package_import = isinstance(node, ast.ImportFrom) and node.level > 0
        is_package_path = (
            path.name == "shared.py"
            and isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SCRIPT_FILE"
                    for target in node.targets)
        )
        if is_package_import or is_package_path:
            removed_lines.update(range(node.lineno, node.end_lineno + 1))

    body = "".join(line for number, line in enumerate(lines, 1) if number not in removed_lines)
    if path.name == "commit.py":
        body = body.replace("str(SCRIPT_FILE)[-20:]", "__file__[-20:]")
    return body.strip() + "\n"


def assemble() -> str:
    sections = ["#!/usr/bin/env python3"]
    sections.extend(_module_source(ROOT / "git" / module) for module in MODULES)
    sections.extend(
        [
            f'GENERATED_SOURCE_STAMP = "{source_stamp()}"',
            f"GENERATED_SOURCE_FILES = {STAMP_FILES!r}",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
    )
    return "\n".join(section.rstrip() for section in sections if section.strip()) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine the modular Git tool into one standalone Python script."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "git_logic.py",
        help="output path (default: git_logic.py beside the original script)",
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

    if args.check:
        if not output.is_file():
            parser.error(f"output file does not exist: {output}")
        if output.read_text(encoding="utf-8") != assemble():
            parser.error(f"generated file is out of date: {output}")
        print(f"Up to date: {output}")
        return 0

    if output.exists() and not args.force:
        parser.error(f"output already exists; pass --force to replace it: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(assemble(), encoding="utf-8", newline="\n")
    output.chmod(output.stat().st_mode | 0o111)
    print(f"Generated: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
