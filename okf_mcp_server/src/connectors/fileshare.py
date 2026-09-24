"""File share connector: a local or mounted folder (NFS, SMB, AFP).

Permissions come from the files themselves (`permissions: posix`): a file
readable by its group grants `group:<group name>`, a world-readable file
grants `*`, and the owner is always granted `user:<owner name>`. Mount
options decide which owner and group the server sees, so check them on the
ingest host. Use `permissions: none` to leave access to `_okf.yaml` rules.
"""

import grp
import hashlib
import pwd
import shutil
import stat
from functools import partial
from pathlib import Path
from typing import Iterator, List, Optional

from okf_mcp_server.src.connectors.base import RemoteItem


def _name(lookup, ident: int) -> str:
    try:
        return lookup(ident)[0]
    except KeyError:
        return str(ident)


def posix_principals(st) -> List[str]:
    """Principals from a file's owner, group and mode bits."""
    principals = [f"user:{_name(pwd.getpwuid, st.st_uid)}"]
    if st.st_mode & stat.S_IRGRP:
        principals.append(f"group:{_name(grp.getgrgid, st.st_gid)}")
    if st.st_mode & stat.S_IROTH:
        principals.append("*")
    return principals


def _copy(src: Path, target: Path) -> None:
    shutil.copy2(src, target)


def list_items(root: Path, permissions: str = "posix") -> Iterator[RemoteItem]:
    """Every regular file under `root` (symlinks are not followed)."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"file share not found or not a directory: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        st = path.stat()
        principals: Optional[List[str]] = (
            posix_principals(st) if permissions == "posix" else None
        )
        version = hashlib.sha256(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[
            :16
        ]
        yield RemoteItem(
            key=rel,
            path=rel,
            version=version,
            size=st.st_size,
            principals=principals,
            download=partial(_copy, path),
        )
