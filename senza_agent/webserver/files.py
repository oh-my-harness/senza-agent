"""File browser: list / read / write / upload / zip.

Provides a filesystem browser API for the web UI. All operations accept
absolute paths; the browser frontend handles navigation.
"""
from __future__ import annotations

import io
import mimetypes
import os
import posixpath
import zipfile
from pathlib import Path
from typing import Any

from aiohttp import web


_MAX_PREVIEW_BYTES = 512 * 1024  # 512 KB text preview cap


def list_dir(dir_path: str) -> dict[str, Any]:
    """List directory contents, dirs first then files, alphabetically."""
    p = Path(dir_path)
    if not p.is_dir():
        return {"error": f"not a directory: {dir_path}"}
    entries = []
    try:
        for entry in p.iterdir():
            try:
                is_dir = entry.is_dir()
                size = 0
                if not is_dir:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass
                entries.append({
                    "name": entry.name,
                    "type": "dir" if is_dir else "file",
                    "size": size,
                    "fullPath": str(entry),
                })
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError) as e:
        return {"error": str(e)}

    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": dir_path, "files": entries}


def read_file(file_path: str) -> dict[str, Any]:
    """Read a file for preview.

    Returns ``{binary, content, truncated, size}`` for text files, or
    ``{binary: true, content: null, size}`` for binary files.
    """
    p = Path(file_path)
    if not p.is_file():
        return {"error": f"not a file: {file_path}"}
    if p.is_dir():
        return {"error": f"is a directory: {file_path}"}

    try:
        size = p.stat().st_size
    except OSError as e:
        return {"error": str(e)}

    # Sniff first 1024 bytes to detect binary
    try:
        with open(p, "rb") as f:
            head = f.read(1024)
    except OSError as e:
        return {"error": str(e)}

    if b"\x00" in head:
        return {"binary": True, "content": None, "truncated": False, "size": size}

    # Read as text (up to cap)
    read_len = min(size, _MAX_PREVIEW_BYTES)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(read_len)
    except OSError as e:
        return {"error": str(e)}

    return {
        "binary": False,
        "content": content,
        "truncated": size > read_len,
        "size": size,
    }


def write_file(file_path: str, content: str) -> dict[str, Any]:
    """Write text content to a file."""
    p = Path(file_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True}
    except OSError as e:
        return {"error": str(e)}


def upload_file(dest_dir: str, rel_path: str, data: bytes) -> dict[str, Any]:
    """Upload a file to ``dest_dir/rel_path``.

    ``rel_path`` is a posix-style relative path; intermediate dirs are
    created. Path traversal is guarded against.
    """
    root = Path(dest_dir).resolve()
    # Resolve the full destination; if rel_path contains .., it may escape root.
    dest = (root / rel_path).resolve()
    try:
        dest.relative_to(root)
    except ValueError:
        return {"error": "invalid rel path (escapes destination dir)"}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "size": len(data)}
    except OSError as e:
        return {"error": str(e)}


def zip_dir(dir_path: str) -> tuple[bytes, str]:
    """Zip a directory tree, returning ``(zip_bytes, filename)``."""
    p = Path(dir_path)
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {dir_path}")

    base = p.name or "folder"
    fname = f"{base}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(p):
            for fname_inner in files:
                full = Path(root) / fname_inner
                arc = full.relative_to(p)
                zf.write(full, arcname=str(arc))
    return buf.getvalue(), fname


def get_roots() -> dict[str, Any]:
    """Return filesystem roots, home, cwd."""
    import sys
    roots = []
    if sys.platform == "win32":
        for c in range(65, 91):
            drive = f"{chr(c)}:\\"
            if os.path.exists(drive):
                roots.append(drive)
    else:
        roots.append("/")
    return {
        "roots": roots,
        "home": os.path.expanduser("~"),
        "cwd": os.getcwd(),
        "sep": os.sep,
    }


def guess_mime(path: str) -> str:
    """Guess MIME type from filename."""
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"
