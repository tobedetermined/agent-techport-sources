"""Files attached to tool results: one set of rules for every source.

Claude Code saves a PDF or image embedded in a tool result to a file and lets
Claude read it page by page (see "Reading PDFs" in docs/design.md). The limits
are Claude Code's, measured; the 20 MB cap was agreed.
"""

import base64
import os
import re
import time

from mcp.types import BlobResourceContents, EmbeddedResource

from .http import SourceError, TooLarge

# Claude Code closes the connection when one tool result is over about 16 MiB
# (measured 2026-09-30: 15.6 MB of encoded files worked, 16.8 MB didn't). Files
# are base64-encoded, a third bigger, so one result carries at most 11 MB of them.
MAX_CALL_BYTES = 11 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024       # agreed cap; files over MAX_CALL_BYTES are saved locally instead
SAVED_FILE_DAYS = 30                    # the plugin deletes its own saved files after this
# Claude Code adds a note of about 345 characters per attached file, whatever
# its size, so the number of files is capped as well as their bytes.
MAX_ATTACHED = 20
MIN_ROOM = 1024 * 1024                  # below this, files of unknown size wait for a later call
FILE_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif"}
EXTENSIONS = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}

OVER_CAP = "over the 20 MB cap; link only"
NOT_PDF_OR_IMAGE = "not a PDF or image"
SAVED = "too large to attach; saved locally. Read it from saved_to (Claude Code may ask the user for permission)"


def cannot_attach(extension, size):
    """Why a file can never be attached, judged from what the source says about
    it (extension, size), or None. Such files needn't be downloaded."""
    ext = (extension or "").lower()
    if ext and ext not in FILE_TYPES:
        return f"{NOT_PDF_OR_IMAGE} (.{ext})"
    if size and size > MAX_FILE_BYTES:
        return OVER_CAP
    return None


def mime(extension, content_type):
    """The type to attach a file as, or None if it isn't a PDF or image."""
    return content_type if content_type in FILE_TYPES.values() else FILE_TYPES.get((extension or "").lower())


def block(uri, data, kind):
    return EmbeddedResource(type="resource", resource=BlobResourceContents(
        uri=uri, mimeType=kind, blob=base64.b64encode(data).decode()))


def save(folder, name, data, kind):
    """Too big to attach: save in the plugin's own folder and return the path.
    Removes that folder's files older than SAVED_FILE_DAYS first."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name or ""):
        raise ValueError(f"Not a plain file name: {name!r}")
    os.makedirs(folder, exist_ok=True)
    cutoff = time.time() - SAVED_FILE_DAYS * 86400
    for old in os.listdir(folder):              # the plugin's own files only
        path = os.path.join(folder, old)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass
    path = os.path.join(folder, f"{name}.{EXTENSIONS.get(kind, 'bin')}")
    with open(path, "wb") as out:
        out.write(data)
    return path


def attach(files, fetch, *, fields, later, folder):
    """Attach files in the order given while the result stays under Claude
    Code's limits. Returns (blocks, report).

    files: dicts with "url", "extension" and "bytes" (None when unknown), and
      "save_as", a plain name for a copy saved locally.
    fetch(file, max_bytes): returns (data, content type); raises TooLarge over
      max_bytes, or SourceError.
    fields: the keys of each file to show in the report.
    later: the reason given for a file left for a later call.
    folder: where a file too big to attach is saved.

    The report lists every file with the same fields: the caller's, then bytes,
    included, reason and saved_to, so it can be sent as a table.
    """
    blocks, report, used = [], [], 0
    for f in files:
        entry = {**{k: f.get(k) for k in fields}, "bytes": f.get("bytes"), "included": False,
                 "reason": None, "saved_to": None}
        why = cannot_attach(f.get("extension"), f.get("bytes"))
        if why:
            report.append({**entry, "reason": why})
            continue
        known = f.get("bytes")
        # A file of unknown size is downloaded only while MIN_ROOM is left, so a spent
        # budget doesn't fetch file after file just to leave each one for later.
        if len(blocks) >= MAX_ATTACHED or (known and known <= MAX_CALL_BYTES and used + known > MAX_CALL_BYTES) \
                or (not known and used and MAX_CALL_BYTES - used < MIN_ROOM):
            report.append({**entry, "reason": later})
            continue
        try:
            data, content_type = fetch(f, MAX_FILE_BYTES)
        except TooLarge:
            report.append({**entry, "reason": OVER_CAP})
            continue
        except SourceError as e:
            report.append({**entry, "reason": str(e)})
            continue
        entry["bytes"] = len(data)
        kind = mime(f.get("extension"), content_type)
        if not kind:
            report.append({**entry, "reason": f"{NOT_PDF_OR_IMAGE} ({content_type})"})
            continue
        if len(data) > MAX_CALL_BYTES:
            report.append({**entry, "saved_to": save(folder, f["save_as"], data, kind), "reason": SAVED})
            continue
        if used + len(data) > MAX_CALL_BYTES:
            report.append({**entry, "reason": later})
            continue
        used += len(data)
        blocks.append(block(f["url"], data, kind))
        report.append({**entry, "included": True})
    return blocks, report
