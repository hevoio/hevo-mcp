"""Cleans what Hevo returns before the agent (and so the model) sees it.

Two things, applied to every tool result:

* Secrets: a value under a key that names a secret (`password`, `secret_key`, `private_key`, `*_token`, ...)
  is replaced with "[redacted]". The model never needs a stored credential, and anything it sees can end
  up in a chat log or another tool call.
* Hidden text: terminal escape sequences, control characters (other than newline, tab and carriage return)
  and invisible or direction-changing Unicode are removed from every string. They can hide instructions
  from the user, rewrite a terminal, or make text read differently from what it says.

Data is otherwise returned as Hevo sent it; the server instructions tell the agent it is untrusted.
"""

import re
from typing import Any

REDACTED = "[redacted]"

# Key names (lower case) whose values are secrets. Exact names, then suffixes.
SECRET_KEYS = frozenset({
    "password", "passwd", "pwd", "passphrase", "secret", "secret_key", "client_secret", "api_secret",
    "api_key", "apikey", "access_key", "access_key_secret", "secret_access_key", "private_key",
    "private_key_passphrase", "ssh_private_key", "access_token", "refresh_token", "auth_token", "bearer_token",
    "session_token", "service_account_key", "credentials_json", "connection_string",
})
SECRET_SUFFIXES = ("_password", "_passwd", "_secret", "_private_key", "_api_key", "_access_key", "_token",
                   "_passphrase", "_secret_key")
# Keys that end like a secret but aren't one.
NOT_SECRET = frozenset({"next_token", "page_token", "next_page_token", "pagination_token", "max_tokens"})
# A secret reference (secrets.py) names a secret instead of holding it, so it is never redacted: the user must
# see which secret a request would send, and to where.
REFERENCE_PREFIXES = ("$secret_file:",)

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
# C0 controls except \t \n \r, DEL and C1 controls; zero-width and joiner characters; bidirectional
# embeddings, overrides and isolates; word joiner and invisible operators; byte-order mark; tag characters.
HIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069"
                    "\ufeff\U000e0000-\U000e007f]")


def is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIXES)


def is_secret_key(key: str) -> bool:
    k = key.strip().lower().replace("-", "_")
    return k not in NOT_SECRET and (k in SECRET_KEYS or k.endswith(SECRET_SUFFIXES))


def clean_text(text: str) -> str:
    if text.isascii() and text.isprintable():
        return text  # the common case: nothing to remove
    return HIDDEN.sub("", ANSI.sub("", text))


def clean(value: Any) -> Any:
    """A copy of `value` with secrets redacted and hidden characters removed from every string."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = clean_text(key) if isinstance(key, str) else key
            secret = isinstance(key, str) and is_secret_key(key) and item not in (None, "", [], {}) and not is_reference(item)
            out[name] = REDACTED if secret and not isinstance(item, (dict, list, bool)) else clean(item)
        return out
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value
