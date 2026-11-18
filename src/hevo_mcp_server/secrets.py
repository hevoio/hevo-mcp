"""Secret references: how a Source or Destination password reaches Hevo without passing through the chat.

Hevo's create endpoints need the real credential in the request (Edge tests both connections before it
saves anything), and the server sends the body the agent built. So instead of the password, the agent
writes a reference, and this module swaps in the value just before the request goes to Hevo:

* `$secret_file:<name>`: the contents of the file `<name>` in the secrets folder, `secrets/` inside the keys
  folder (`~/.hevo/secrets`, or `$HEVO_KEYS_DIR/secrets`). A bare file name only, never a path, so nothing
  outside that folder can be read. The file holds the plain value and nothing else (no quotes, no `key=`
  prefix, no JSON wrapper; a multi-line value such as a PEM key as written) and must be readable by its
  owner only, like a key file. Whitespace around the value (the final newline, a pasted space) is trimmed.

Three rules keep a reference from becoming a way to leak a secret:

* A reference is accepted only under a key that names a secret (`password`, `*_token`, `client_secret`,
  ...; sanitize.py's list): where Hevo stores it as a secret and where this server redacts it in results.
  Under any other key it is refused, so a value can't come back in a response or end up in a hostname.
  Only files are read, never environment variables, so a prompt-injected request can't send the user's
  other credentials (cloud keys, tokens) anywhere.
* Every request that carries a reference is shown to the user for approval, whatever the endpoint, so they
  see which secret goes to which host before the connection test sends it.
* The value is read when the call is made and is never returned, logged, echoed in an error or written
  anywhere by this server; the chat, the approval prompt and the logs only ever see the reference.

This works for a local server (stdio): it runs on the user's machine and reads the user's own files and
environment. A shared HTTP server can't, so there a reference is refused and nothing is sent to Hevo.
"""

import re
from pathlib import Path
from typing import Any

from hevo_mcp_server.auth import (
    PrivateFileError,
    keys_dir,
    private_dir_hint,
    private_file_hint,
    read_private_file,
)
from hevo_mcp_server.sanitize import REFERENCE_PREFIXES, is_reference, is_secret_key

(FILE_PREFIX,) = REFERENCE_PREFIXES
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_BYTES = 65_536  # a service-account JSON is a few KB; anything bigger isn't a credential


class SecretUnavailable(Exception):
    """The reference can't be resolved; the message names the reference, never a value."""


def secrets_dir() -> Path:
    """`secrets/` next to the key file: ~/.hevo/secrets, or $HEVO_KEYS_DIR/secrets."""
    return keys_dir() / "secrets"


def references(value: Any) -> list[str]:
    """Every reference string in `value` (a request body), in order, without duplicates."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if is_reference(v):
            if v not in out:
                out.append(v)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    return out


def bad_name(refs: list[str]) -> str | None:
    """The first reference whose name isn't a bare file name, so it is refused before the approval prompt."""
    return next((ref for ref in refs if not _valid_name(ref[len(FILE_PREFIX):])), None)


def _valid_name(name: str) -> bool:
    return bool(FILE_NAME.fullmatch(name)) and ".." not in name


def _name_error(ref: str) -> str:
    return (f"{ref!r}: use a bare file name (letters, digits, '.', '_', '-'), not a path. "
            f"Files are read from {secrets_dir()} only.")


def misplaced(value: Any) -> list[str]:
    """Fields holding a reference under a key that doesn't name a secret, as dotted paths."""
    out: list[str] = []

    def walk(v: Any, where: str, secret: bool) -> None:
        if is_reference(v):
            if not secret:
                out.append(where or "body")
        elif isinstance(v, dict):
            for k, item in v.items():
                walk(item, f"{where}.{k}" if where else str(k), isinstance(k, str) and is_secret_key(k))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{where}[{i}]", secret)

    walk(value, "", False)
    return out


def resolve(value: Any) -> Any:
    """A copy of `value` with every reference replaced by the secret it names.

    Raises SecretUnavailable, naming the reference and how to fix it, if any can't be read."""
    if is_reference(value):
        return _lookup(value)
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v) for v in value]
    return value


def _lookup(ref: str) -> str:
    name = ref[len(FILE_PREFIX):]
    folder = secrets_dir()
    if not _valid_name(name):
        raise SecretUnavailable(_name_error(ref))
    path = folder / name
    if not path.exists() and not path.is_symlink():
        raise SecretUnavailable(f"{ref!r}: no file named {name} in {folder}. Ask the user to save the secret "
                                f"there as a file holding the plain value and nothing else ({private_dir_hint(folder)}; "
                                f"then the file, {private_file_hint(path)}), then retry.")
    try:
        # Opened without following links and checked on the open handle (owner, mode, a regular file), so a
        # link or a swap between the check and the read can't point the server at another file. Whitespace
        # around the value is never part of a credential: a final newline from the editor, a space pasted
        # along with the password. A multi-line secret (a service-account JSON, a PEM key) keeps its inner
        # lines; only the ends are trimmed.
        value = read_private_file(path, MAX_BYTES).strip()
    except PrivateFileError as exc:
        if exc.code == "mode":
            raise SecretUnavailable(f"{ref!r}: {path} is readable by other users. Run {private_file_hint(path)} on "
                                    "it, and then retry.") from None
        raise SecretUnavailable(f"{ref!r}: {path} {exc}") from None
    if not value:
        raise SecretUnavailable(f"{ref!r}: {path} is empty.")
    return value
