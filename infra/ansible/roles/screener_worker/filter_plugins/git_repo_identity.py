"""Compare Git remotes by the repository they name, not how they spell it."""

import re
from urllib.parse import urlsplit

# scp-like syntax: [user@]host:owner/repo
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^/:]+):(.*)$")


def git_repo_identity(remote):
    """Return ``host/owner/repo`` for a remote URL.

    Transport (ssh, https, scp-like), user, port, case, a trailing ``.git`` and
    trailing slashes do not change the identity, so the SSH and HTTPS remotes of
    one repository compare equal while a different repository does not.
    """
    remote = str(remote).strip().lower()
    if "://" in remote:
        parts = urlsplit(remote)
        host, path = parts.hostname or "", parts.path
    elif match := _SCP_REMOTE.match(remote):
        host, path = match.groups()
    else:
        host, path = "", remote
    path = path.strip("/").removesuffix(".git").rstrip("/")
    return f"{host}/{path}" if host else path


class FilterModule:
    def filters(self):
        return {"git_repo_identity": git_repo_identity}
