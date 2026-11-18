"""Whether this install runs on the dependency versions the release was tested with.

The release's `constraints.txt` (every dependency at its uv.lock version) ships inside the package. Installs
made with `-c constraints.txt` (the plugins, the documented commands) match it exactly; a plain `uvx` or
`pip install` may resolve newer transitive versions. `hevo_whoami` and the start log say which it is, so
support can tell at once whether a problem install is on the tested set. Nothing here changes what runs.
"""

import re
from importlib import metadata, resources

from packaging.markers import Marker

PINS_FILE = "constraints.txt"
# `name==version`, optionally followed by `; <environment marker>` (pywin32 on Windows, for example).
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)\s*(?:;\s*([^#]+?))?\s*(?:#.*)?$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503 name normalisation


def tested_versions() -> dict[str, str]:
    """{normalised name: version} from the shipped constraints file, for this platform; empty if the file isn't there."""
    try:
        text = resources.files("hevo_mcp_server").joinpath(PINS_FILE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    out = {}
    for line in text.splitlines():
        if m := _PIN.match(line):
            if m.group(3) and not _applies(m.group(3)):
                continue  # pinned for another platform or interpreter, so not expected here
            out[_norm(m.group(1))] = m.group(2)
    return out


def _applies(marker: str) -> bool:
    try:
        return bool(Marker(marker).evaluate())
    except Exception:  # noqa: BLE001 - an odd marker means "don't know": don't report false drift
        return False


def dependency_status() -> dict[str, object]:
    """`{"status": "as_tested"}`, or the packages whose installed version differs from the tested one."""
    tested = tested_versions()
    if not tested:
        return {"status": "unknown", "hint": f"{PINS_FILE} isn't shipped with this build"}
    drift: dict[str, dict[str, str | None]] = {}
    for name, version in tested.items():
        try:
            installed: str | None = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed != version:
            drift[name] = {"tested": version, "installed": installed}
    if not drift:
        return {"status": "as_tested"}
    return {"status": "drifted", "drift": drift,
            "hint": "This install isn't on the dependency versions the release was tested with. To match them, "
                    "install with `-c constraints.txt` (the release's file) as the README shows."}
