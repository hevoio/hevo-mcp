"""Cleans what Hevo returns before the agent (and so the model) sees it.

Two things, applied to every tool result:

* Secrets: a value under a key that names a secret (`password`, `secret_key`, `private_key`, `*_token`, ...)
  is replaced with "[redacted]". The model never needs a stored credential, and anything it sees can end
  up in a chat log or another tool call.
* Request headers: a REST API Source keeps its HTTP headers (`Authorization: Bearer ...`, API tokens) under
  `headers`, often as free text that no key name marks as secret. Every header value under a `headers` (or
  `*_headers`) key is redacted, in whatever shape it comes (text, JSON text, an object, a list of name and
  value pairs); header names are kept so the agent can still say which headers a Source sends. A name and
  value pair anywhere else (`{"name": "Authorization", "value": ...}`) is redacted when its name names a
  secret.
* Credentials in URLs: in any string, a query parameter whose name names a secret (`?api_key=...`,
  `&access_token=...`, `&sig=...`) has its value redacted, and so does the password in a URL's user info
  (`scheme://user:password@host`). The rest of the URL is kept.
* Hidden text: terminal escape sequences, control characters (other than newline, tab and carriage return)
  and invisible or direction-changing Unicode are removed from every string. They can hide instructions
  from the user, rewrite a terminal, or make text read differently from what it says.

Data is otherwise returned as Hevo sent it; the server instructions tell the agent it is untrusted.
"""

import json
import re
from typing import Any

REDACTED = "[redacted]"

# Key names (lower case) whose values are secrets. Exact names, then suffixes.
SECRET_KEYS = frozenset({
    "password", "passwd", "pwd", "token", "key", "passphrase", "secret", "secret_key", "client_secret", "api_secret",
    "api_key", "apikey", "access_key", "access_key_secret", "secret_access_key", "private_key",
    "private_key_passphrase", "ssh_private_key", "access_token", "refresh_token", "auth_token", "bearer_token",
    "session_token", "service_account_key", "credentials_json", "connection_string",
})
SECRET_SUFFIXES = ("_password", "_passwd", "_secret", "_private_key", "_api_key", "_access_key", "_token",
                   "_passphrase", "_secret_key")
# Keys that end like a secret but aren't one.
NOT_SECRET = frozenset({"next_token", "page_token", "next_page_token", "pagination_token", "max_tokens"})
# Header names whose values are credentials. They are redacted in results but are not secret field names for
# secrets.py: a `$secret_file:` reference still resolves only under SECRET_KEYS.
HEADER_SECRETS = frozenset({"authorization", "proxy_authorization", "cookie", "set_cookie"})
# Query parameter names that carry a credential although they don't end like a secret (signed URLs).
QUERY_SECRETS = frozenset({"sig", "signature", "x_amz_signature", "x_amz_credential", "x_goog_signature",
                           "x_goog_credential", "auth", "authorization"})
# `name=value` after `?`, `&` or `;`; the value runs to the next separator, quote or space, leaving out
# punctuation that ends a sentence (`...?key=abc: 401 Unauthorized`).
QUERY_PARAM = re.compile(r"([?&;])([A-Za-z0-9_.\-]+)=([^&;#\s\"'<>]*?)(?=[.,:)]*(?:[&;#\s\"'<>]|$))")
# The password in `scheme://user:password@host`.
URL_PASSWORD = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@\"']*:)([^\s/@\"']+)(@)")
# The field names that hold a pair's name and value in a list of pairs (`[{"key": "Authorization", "value": ...}]`).
PAIR_NAMES = ("name", "key", "header", "header_name")
PAIR_VALUES = ("value", "header_value")
# One header per line, `Name: value` (RFC 9110 token characters in the name).
HEADER_LINE = re.compile(r"^(\s*[!#$%&'*+.^_`|~0-9A-Za-z-]+\s*:)(.*)$")
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


def _normal(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _redacts(key: str) -> bool:
    """Whether a result value under this key is a credential (a secret field or a credential header)."""
    return is_secret_key(key) or _normal(key) in HEADER_SECRETS


def _is_headers(key: str) -> bool:
    k = _normal(key)
    return k == "headers" or k.endswith("_headers")


def _pair(value: dict) -> tuple[str, str] | None:
    """The (name field, value field) of a name and value pair, if `value` is one."""
    name = next((k for k in PAIR_NAMES if isinstance(value.get(k), str)), None)
    val = next((k for k in PAIR_VALUES if k in value), None)
    return (name, val) if name and val else None


def _empty(value: Any) -> bool:
    return value in (None, "", [], {}) or is_reference(value)


def _headers(value: Any) -> Any:
    """`value` from under a headers key, with every header value redacted and the names kept."""
    if isinstance(value, str):
        if _empty(value):
            return value
        lines = [line for line in value.splitlines() if line.strip()]
        if lines and all(HEADER_LINE.match(line) for line in lines):
            return "\n".join(HEADER_LINE.sub(r"\1 " + REDACTED, line) for line in lines)
        try:
            parsed = json.loads(value)
        except ValueError:
            return REDACTED
        return json.dumps(_headers(parsed)) if isinstance(parsed, (dict, list)) else REDACTED
    if isinstance(value, dict):
        if pair := _pair(value):
            name, val = pair
            return {**clean(value), val: value[val] if _empty(value[val]) else REDACTED, name: clean_text(value[name])}
        return {clean_text(k) if isinstance(k, str) else k: item if _empty(item) else _headers(item)
                for k, item in value.items()}
    if isinstance(value, list):
        return [_headers(item) for item in value]
    return value if value is None or isinstance(value, bool) else REDACTED


def _query_secret(name: str) -> bool:
    return _redacts(name) or _normal(name) in QUERY_SECRETS


def url_secrets(text: str) -> str:
    """`text` with credentials in any URL it holds redacted: secret query parameters and user-info passwords."""
    if "=" in text:
        text = QUERY_PARAM.sub(lambda m: f"{m[1]}{m[2]}={REDACTED}" if m[3] and _query_secret(m[2]) else m[0],
                               text)
    if "@" in text:
        text = URL_PASSWORD.sub(lambda m: f"{m[1]}{REDACTED}{m[3]}", text)
    return text


def clean_text(text: str) -> str:
    if text.isascii() and text.isprintable():
        return text  # the common case: nothing to remove
    return HIDDEN.sub("", ANSI.sub("", text))


def clean(value: Any) -> Any:
    """A copy of `value` with secrets redacted and hidden characters removed from every string."""
    if isinstance(value, str):
        return url_secrets(clean_text(value))
    if isinstance(value, dict):
        pair = _pair(value)
        out = {}
        for key, item in value.items():
            name = clean_text(key) if isinstance(key, str) else key
            if pair and key == pair[0]:
                out[name] = clean_text(item)  # a pair's name (`"key": "Authorization"`) is a label, not a secret
                continue
            if pair and key == pair[1] and _redacts(value[pair[0]]) and not _empty(item):
                out[name] = REDACTED
                continue
            if isinstance(key, str) and _is_headers(key):
                out[name] = _headers(item)
                continue
            secret = isinstance(key, str) and _redacts(key) and not _empty(item)
            out[name] = REDACTED if secret and not isinstance(item, (dict, list, bool)) else clean(item)
        return out
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value
