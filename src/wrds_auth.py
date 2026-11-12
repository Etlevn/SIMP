"""WRDS authentication and session helpers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path

try:
    import wrds
except ImportError:  # pragma: no cover
    wrds = None

from paths import PROJECT_ROOT

WRDS_PGPASS_HOST = "wrds-pgdata.wharton.upenn.edu:9737:wrds"
_DOTENV_LOADED = False


def load_project_dotenv(path: Path | None = None, *, override: bool = False) -> Path | None:
    """Load ``KEY=VALUE`` pairs from the project ``.env`` into ``os.environ``.

    By default does not overwrite variables already present in the environment.
    Returns the path loaded, or ``None`` if the file is missing.
    """
    global _DOTENV_LOADED
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    _DOTENV_LOADED = True
    return env_path


def _get_wrds_credential(value, key, *, secret=False):
    """Explicit argument, then environment variables / interactive prompt."""
    if value:
        return value
    value = os.environ.get(key)
    if not value and key == "WRDS_USERNAME":
        value = os.environ.get("WRDS_USER")
    if not value:
        value = getpass(f"{key}: ") if secret else input(f"{key}: ").strip()
    if not value:
        raise ValueError(f"{key} must not be empty.")
    return value


def setup_wrds_credentials(
    username=None, password=None, user_key="WRDS_USERNAME", pass_key="WRDS_PASSWORD"
):
    """Configure credentials via env/prompt and write the PostgreSQL password file.

    Loads project ``.env`` first (without overriding existing env vars). Explicit
    arguments still win. Credentials go to the libpq password file used by WRDS,
    never into source or output.

    Returns ``(username, password)``.
    """
    load_project_dotenv()
    username = _get_wrds_credential(username, user_key)
    password = _get_wrds_credential(password, pass_key, secret=True)
    if any(char in username + password for char in "\r\n"):
        raise ValueError("Credentials must not contain newline characters.")
    os.environ["WRDS_USER"] = username

    default_path = (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "postgresql"
        / "pgpass.conf"
        if os.name == "nt"
        else Path.home() / ".pgpass"
    )
    pgpass_path = Path(os.environ.get("PGPASSFILE", str(default_path))).expanduser()
    pgpass_path.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if pgpass_path.exists():
        kept = [
            ln
            for ln in pgpass_path.read_text().splitlines()
            if ln.strip() and not ln.startswith(WRDS_PGPASS_HOST + ":")
        ]

    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")

    kept.append(f"{WRDS_PGPASS_HOST}:{escape(username)}:{escape(password)}")
    with pgpass_path.open("w") as f:
        if os.name != "nt":
            os.chmod(pgpass_path, 0o600)
        f.write("\n".join(kept) + "\n")
    os.environ["PGPASSFILE"] = str(pgpass_path)

    print("✓ WRDS login environment configured.")
    return username, password


def connect_wrds(username=None, password=None):
    print("Connecting to WRDS...")
    if wrds is None:
        raise ImportError("wrds package is not installed; install project dependencies.")
    username, password = setup_wrds_credentials(username=username, password=password)
    db = wrds.Connection(
        autoconnect=False,
        wrds_username=username,
        wrds_password=password,
    )
    db._Connection__make_sa_engine_conn(raise_err=True)
    db.load_library_list()
    return db


def disconnect_wrds(db):
    db.close()
    print("✓ WRDS database connection closed.")


@contextmanager
def wrds_session(username=None, password=None):
    """with wrds_session(username, password) as db: ... (always closes)."""
    db = connect_wrds(username=username, password=password)
    try:
        yield db
    finally:
        disconnect_wrds(db)


__all__ = [
    "WRDS_PGPASS_HOST",
    "connect_wrds",
    "disconnect_wrds",
    "load_project_dotenv",
    "setup_wrds_credentials",
    "wrds_session",
]
