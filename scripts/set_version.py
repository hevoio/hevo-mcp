"""Set the release version everywhere it appears, so a release is one command.

    uv run python scripts/set_version.py 1.1.0
    uv run python scripts/set_version.py --check        # only report the version in each file
    uv run python scripts/set_version.py --constraints  # only rewrite constraints.txt (after changing dependencies)

It changes the version in pyproject.toml and every plugin manifest, and the release tag and PyPI version that
the Codex marketplace, the Codex server command and the README install from; then it runs
`uv lock` so uv.lock follows. Then: commit, merge, tag the merged commit `v<version>` and push the tag (see
"Releasing" in CONTRIBUTING.md).
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"\d+\.\d+\.\d+")
PYPI_PIN = r"(hevo-mcp==)(\d+\.\d+\.\d+)()"  # install the release from PyPI
PINS_URL = r"(raw\.githubusercontent\.com/hevoio/hevo-mcp/v)(\d+\.\d+\.\d+)(/constraints\.txt)"  # its pins

# (file, pattern with the version as group 2) for every place a release changes.
PLACES = [
    ("pyproject.toml", r'(^version = ")(\d+\.\d+\.\d+)(")'),
    (".claude-plugin/plugin.json", r'("version": ")(\d+\.\d+\.\d+)(")'),
    (".codex-plugin/plugin.json", r'("version": ")(\d+\.\d+\.\d+)(")'),
    (".claude-plugin/marketplace.json", r'("ref": "v)(\d+\.\d+\.\d+)(")'),  # installs the release tag
    (".agents/plugins/marketplace.json", r'("ref": "v)(\d+\.\d+\.\d+)(")'),
    (".codex-mcp.json", PYPI_PIN),
    (".codex-mcp.json", PINS_URL),
    ("README.md", PYPI_PIN),
    ("README.md", PINS_URL),
]


def write_constraints() -> None:
    """constraints.txt: every dependency at its uv.lock version. Plugins and the documented uvx
    commands install with it (`-c`), so customers get exactly what CI tested. A copy ships inside the
    package (src/hevo_mcp_server/constraints.txt), so hevo_whoami can say whether an install matches it."""
    subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--no-header",
                    "--no-annotate", "-q", "-o", "constraints.txt"], cwd=ROOT, check=True)
    shutil.copyfile(ROOT / "constraints.txt", ROOT / "src" / "hevo_mcp_server" / "constraints.txt")


def found(text: str, pattern: str) -> list[str]:
    return [m.group(2) for m in re.finditer(pattern, text, re.MULTILINE)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", nargs="?", help="the new version, e.g. 1.1.0")
    parser.add_argument("--check", action="store_true", help="only report the current versions")
    parser.add_argument("--constraints", action="store_true", help="only rewrite constraints.txt from uv.lock")
    args = parser.parse_args()
    if args.constraints:
        write_constraints()
        print("constraints.txt rewritten from uv.lock")
        return 0

    current = [(name, pattern, found((ROOT / name).read_text(), pattern)) for name, pattern in PLACES]
    missing = sorted({name for name, _, versions in current if not versions})
    if missing:
        sys.exit(f"no version found in {', '.join(missing)}: update PLACES in this script")
    by_file: dict[str, set[str]] = {}
    for name, _, versions in current:
        by_file.setdefault(name, set()).update(versions)
    if args.check or not args.version:
        for name, versions in by_file.items():
            print(f"{name:<36} {', '.join(sorted(versions))}")
        distinct = set().union(*by_file.values())
        print("in step" if len(distinct) == 1 else f"NOT in step: {', '.join(sorted(distinct))}")
        return 0 if len(distinct) == 1 else 1

    if not VERSION.fullmatch(args.version):
        sys.exit(f"{args.version!r} isn't a version like 1.1.0")
    for name, pattern in PLACES:
        path = ROOT / name
        path.write_text(re.sub(pattern, lambda m: m.group(1) + args.version + m.group(3), path.read_text(),
                               flags=re.MULTILINE))
    for name, versions in by_file.items():
        print(f"{name:<36} {', '.join(sorted(versions))} -> {args.version}")
    subprocess.run(["uv", "lock", "-q"], cwd=ROOT, check=True)
    write_constraints()
    print(f"uv.lock and constraints.txt updated. Next: commit, merge, then tag the merged commit v{args.version} "
          "and push the tag.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
