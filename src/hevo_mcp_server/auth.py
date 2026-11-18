"""Who is calling, and with which Hevo API key.

Two modes:

* stdio (local): the person running the server is the only caller. The Hevo key is the file
  `hevo-key.json` in the keys folder (`~/.hevo`, or HEVO_KEYS_DIR): the key works in the region of the user's
  Hevo account. There is no default region: every call names the region with its `region`
  argument, and the agent asks the user which region their account is in when it doesn't know.
* http (shared/remote): every caller brings their own Hevo key as
  `Authorization: Bearer <base64(api_key:api_secret)>` and picks the region with
  the `X-Hevo-Region` header (or a call's `region` argument). The server stores no Hevo
  credentials and never falls back to a local key, so each call runs with exactly the
  permissions of the caller's own key.
"""

import base64
import binascii
import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_headers

REGIONS = {
    "asia": "https://asia.hevodata.com",
    "au": "https://au.hevodata.com",
    "eu": "https://eu.hevodata.com",
    "eu2": "https://eu2.hevodata.com",
    "in": "https://in.hevodata.com",
    "us": "https://us.hevodata.com",
    "us-gcp": "https://us-gcp.hevodata.com",
    "us2": "https://us2.hevodata.com",
}
# Which products a region offers, per Hevo's public regions page: eu2 (London) has Edge Pipelines only, and
# us-gcp (N. Virginia on Google Cloud) has Standard Pipelines only. The team's users and OAuth accounts are
# shared, so the account endpoints answer everywhere. Checked live: the Edge API is absent on us-gcp (404),
# and the Standard API on eu2 answers with the eu account's data, so a Standard call on eu2 is refused here
# rather than quietly served from another region.
ALL_PRODUCTS = ("edge", "standard", "account")
REGION_PRODUCTS = {"eu2": ("edge", "account"), "us-gcp": ("standard", "account")}
PRODUCT_NAMES = {"edge": "Edge Pipelines", "standard": "Standard Pipelines", "account": "team users and OAuth accounts"}


def region_products(region: str) -> tuple[str, ...]:
    return REGION_PRODUCTS.get(region, ALL_PRODUCTS)


def region_unsupported(region: str, product: str) -> str | None:
    """Why a call for `product` can't be made in `region`, or None when the region offers it."""
    offered = region_products(region)
    if product in offered:
        return None
    only = " and ".join(PRODUCT_NAMES[p] for p in offered if p != "account")
    return (f"Hevo does not offer {PRODUCT_NAMES[product]} in region {region!r} ({only} only there). Ask the user "
            f"which region holds their {PRODUCT_NAMES[product]}, and pass that region.")
REGION_HEADER = "x-hevo-region"
SCOPE_HEADER = "x-hevo-scope"
SCOPES = ("read", "write", "delete")
PROD_KEY_FILE = "hevo-key.json"

REGION_HELP = (
    "No region given. Ask the user which Hevo region their account is in, and pass it as `region` on every call. "
    "The region is the subdomain of the URL at which the user signs in, such as us for us.hevodata.com. Use one "
    "of these regions: " + ", ".join(list(REGIONS)[:-1]) + ", or " + list(REGIONS)[-1] + ". Do not guess."
)


def regions() -> dict[str, str]:
    """Every region this server knows: Hevo's public regions."""
    return dict(REGIONS)


@dataclass(frozen=True)
class HevoCredentials:
    basic_token: str  # base64(api_key:api_secret), sent to Hevo as Basic auth
    base_url: str
    region: str
    scope: str = "read"  # ceiling for this connection: read < write < delete
    key_source: str = ""  # where a local server found the key (the file name), for hevo_whoami

    @property
    def read_only(self) -> bool:
        return self.scope == "read"

    @property
    def caller_id(self) -> str:
        """Stable, non-reversible id for logs and limits: the same key gives the same id within one server
        process."""
        return caller_id(self.basic_token)

    def __repr__(self) -> str:  # never leak the token through logs or tracebacks
        return f"HevoCredentials(region={self.region!r}, key_from={self.key_source!r}, scope={self.scope})"


def region_url(region: str) -> str:
    try:
        return REGIONS[region.strip().lower()]
    except KeyError:
        raise ToolError(f"Unknown Hevo region {region!r}. Use one of these regions: {', '.join(REGIONS)}.") from None


def encode_basic(api_key: str, api_secret: str) -> str:
    return base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()


def decode_basic(token: str) -> tuple[str, str] | None:
    try:
        key, sep, secret = base64.b64decode(token, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return (key, secret) if sep and key and secret else None


def keys_dir() -> Path:
    """The folder that holds the key file and the secrets folder: HEVO_KEYS_DIR, or ~/.hevo."""
    return Path(os.environ.get("HEVO_KEYS_DIR", "").strip() or Path.home() / ".hevo").expanduser()


def key_file() -> Path:
    return keys_dir() / PROD_KEY_FILE


def _windows() -> bool:
    return os.name == "nt"


def private_file_hint(file: Path) -> str:
    """The command that makes a file readable only by the user, for this machine's shell (README: Key file)."""
    if _windows():
        return f'PowerShell: icacls "{file}" /inheritance:r /grant:r "${{env:USERNAME}}:F"'
    return f"chmod 600 {file}"


def private_dir_hint(folder: Path) -> str:
    """The commands that create a folder only the user can open, for this machine's shell."""
    if _windows():
        return (f'PowerShell: New-Item -ItemType Directory -Force "{folder}"; '
                f'icacls "{folder}" /inheritance:r /grant:r "${{env:USERNAME}}:(OI)(CI)F"')
    return f"mkdir -p {folder} && chmod 700 {folder}"


def no_key_help() -> str:
    return (f"No Hevo API key file at {key_file()}. Ask the user to create an API key at "
            "https://<region>.hevodata.com/account/api-keys (click + New API Key, and then Download). The Secret "
            f"Key is shown only once. Ask them to save the downloaded JSON file as {key_file()}, readable only by "
            f"them ({private_file_hint(key_file())}). The key works in the region of their Hevo account, which you "
            "pass as `region` on every call. Never ask for, and never paste, the Access Key or Secret Key in the chat.")


class PrivateFileError(Exception):
    """Why a file isn't a safe private file; the message completes "The ... file <path> ...". `code` is
    "mode" when other users can read it, so a caller can word that case its own way."""

    def __init__(self, message: str, code: str = "other") -> None:
        super().__init__(message)
        self.code = code


def read_private_file(file: Path, max_bytes: int = 64 * 1024) -> str:
    """Reads a file that must belong to this user alone (a key, a secret). The open never follows a symlink,
    and the owner, the mode and the kind of file are checked on the open handle, so a link or a swap
    between the check and the read can't point the server at another file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(file, flags)
    except FileNotFoundError:
        raise PrivateFileError("does not exist.") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP or file.is_symlink():
            raise PrivateFileError("is a symbolic link; save the file itself there instead.") from None
        raise PrivateFileError(f"can't be opened ({exc.__class__.__name__}).") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PrivateFileError("is not a regular file.")
        if os.name == "posix":
            if st.st_uid != os.getuid():
                raise PrivateFileError("belongs to another user; it must be owned by you.")
            if st.st_mode & 0o077:
                raise PrivateFileError(f"can be read by other users on this machine. Restrict it with: "
                                       f"{private_file_hint(file)}", code="mode")
        if st.st_size > max_bytes:
            raise PrivateFileError(f"is larger than {max_bytes:,} bytes, so it can't be a credential.")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            return fh.read()
    except UnicodeDecodeError:
        raise PrivateFileError("is not a text file.") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _read_credentials_file(file: Path) -> tuple[str, str]:
    # Like ssh with a private key: refuse a key file other users on this machine can read, and never follow a
    # symlink (a link planted in the folder could point the server at another file). The file is opened
    # without following links and checked through the open handle, so nothing can be swapped between the
    # check and the read.
    try:
        text = read_private_file(file)
    except PrivateFileError as exc:
        raise ToolError(f"The API key file {file} {exc}") from None
    try:
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ToolError(f"The API key file {file} can't be read as JSON ({exc.__class__.__name__}). It should be "
                        "the file downloaded from the Hevo API Keys page, unchanged.") from None
    lowered = {str(k).lower(): v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
    key = next((lowered[k] for k in ("api_key", "access_key", "key", "username") if lowered.get(k)), None)
    secret = next((lowered[k] for k in ("secret_key", "api_secret", "secret", "password") if lowered.get(k)), None)
    if not key or not secret or key == secret:
        raise ToolError(f"The key file {file} does not contain an Access Key and a Secret Key. It should be the "
                        "file downloaded from the API Keys page in Hevo, unchanged.")
    return key, secret


def configured_regions() -> list[str]:
    """Every region a local server can call."""
    return list(REGIONS)


def credentials_from_env(scope: str = "read", region: str | None = None) -> HevoCredentials:
    """The key and base URL for one call. `region` is required: there is no default. The key is the
    `hevo-key.json` file in the keys folder."""
    wanted = (region or "").strip().lower()
    if not wanted:
        raise ToolError(REGION_HELP)
    base_url = region_url(wanted)
    file = key_file()
    if not file.is_file():
        raise ToolError(no_key_help())
    pair = _read_credentials_file(file)
    return HevoCredentials(encode_basic(*pair), base_url, wanted, scope, file.name)


def credentials_from_request(access_token: AccessToken, default_scope: str = "read",
                             allowed_regions: tuple[str, ...] = (), region: str | None = None) -> HevoCredentials:
    headers = get_http_headers()
    region = (region or "").strip() or headers.get(REGION_HEADER, "")
    if not region:
        raise ToolError(f"Send the `X-Hevo-Region` header ({', '.join(allowed_regions or REGIONS)}).")
    if allowed_regions and region.strip().lower() not in allowed_regions:
        # Multi-region: each Hevo region has its own MCP deployment, so data is processed where it lives.
        raise ToolError(f"This Hevo MCP endpoint serves region {', '.join(allowed_regions)}, not "
                        f"{region.strip().lower()!r}. Connect to the Hevo MCP endpoint for your region "
                        "(the same region as your Hevo account URL).")
    scope = headers.get(SCOPE_HEADER, "").strip().lower() or default_scope
    if scope not in SCOPES:
        raise ToolError(f"X-Hevo-Scope must be one of {', '.join(SCOPES)}, got {scope!r}.")
    return HevoCredentials(access_token.token, region_url(region), region.strip().lower(), scope)


# A fresh salt per process: the id can't be matched against a hash of a guessed key, and it still ties
# every call of one key together for the life of the process (each replica has its own).
_CALLER_SALT = os.urandom(16)


def caller_id(token: str) -> str:
    return hashlib.sha256(_CALLER_SALT + token.encode()).hexdigest()[:16]


class HevoKeyVerifier(TokenVerifier):
    """Accepts `Bearer base64(api_key:api_secret)`; Hevo itself checks the key on each call."""

    async def verify_token(self, token: str) -> AccessToken | None:
        pair = decode_basic(token)
        if pair is None:
            return None
        caller = caller_id(token)  # same as HevoCredentials.caller_id
        return AccessToken(
            token=token,
            client_id=f"hevo-key-{caller}",  # a hash, never part of the key
            scopes=[],
            claims={"sub": caller},
        )


class CredentialResolver:
    """Picks the caller's credentials for the transport the server runs on."""

    def __init__(self, remote: bool, default_scope: str = "read", allowed_regions: tuple[str, ...] = ()):
        self.remote = remote
        self.default_scope = default_scope
        self.allowed_regions = allowed_regions

    def current(self, region: str | None = None) -> HevoCredentials:
        """The caller's credentials for the region the call names (locally required; over HTTP the header's
        region is the fallback)."""
        if not self.remote:
            return credentials_from_env(self.default_scope, region)
        token = get_access_token()
        if token is None:
            raise ToolError("No Hevo API key in this request: send `Authorization: Bearer <base64 of key:secret>` "
                            "and `X-Hevo-Region: <region>`. Never paste the key or secret into the chat.")
        return credentials_from_request(token, self.default_scope, self.allowed_regions, region)

    def regions(self) -> list[str]:
        """Regions a call may name: the public ones locally; over HTTP, the header's region decides."""
        return [] if self.remote else configured_regions()
