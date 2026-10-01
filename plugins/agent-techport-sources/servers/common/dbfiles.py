"""Local SQLite copies that refresh safely while other sessions read them.

Each build writes a new, uniquely named file and then switches a small pointer
file to it. Nothing is ever replaced in place, because Windows can't replace
or delete a file another process has open (a second Claude session reading the
copy). Readers open whatever the pointer names at the moment they query.
Standard library only.
"""

import os
import sqlite3
import time
import uuid

POINTER_NAME = "current.txt"


def connect_ro(path):
    """Read-only, and never creates an empty file if the database has gone."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def read_meta(path):
    try:
        db = connect_ro(path)
    except sqlite3.OperationalError:
        return None
    try:
        return dict(db.execute("SELECT key, value FROM meta"))
    except sqlite3.DatabaseError:
        return None
    finally:
        db.close()


def current_path(data_dir):
    """The database the pointer names, or None before the first build."""
    try:
        with open(os.path.join(data_dir, POINTER_NAME), encoding="utf-8") as f:
            name = f.read().strip()
    except FileNotFoundError:
        return None
    path = os.path.join(data_dir, name)
    return path if name and os.path.exists(path) else None


def open_current(data_dir, missing_error):
    """(connection, meta) for the current database. Retries once if the file
    disappears between reading the pointer and opening it."""
    for attempt in range(2):
        path = current_path(data_dir)
        if path:
            try:
                db = connect_ro(path)
                return db, dict(db.execute("SELECT key, value FROM meta"))
            except sqlite3.DatabaseError:
                if attempt == 1:
                    raise
    raise missing_error


def build(data_dir, prefix, fill):
    """Create a new database with fill(path), then point to it. Returns its path.

    fill must create the whole database, including its meta table. The file is
    named <prefix>-<time>-<random>.db, and is called .building until complete,
    so cleanup by another session never removes a load in progress.
    """
    os.makedirs(data_dir, exist_ok=True)
    name = f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.db"
    path = os.path.join(data_dir, name)
    building = path + ".building"
    try:
        fill(building)
        os.replace(building, path)      # nobody else has this file open, so this works on Windows
    except BaseException:
        if os.path.exists(building):
            os.remove(building)
        raise
    pointer = os.path.join(data_dir, POINTER_NAME)
    tmp = f"{pointer}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(name)
    for attempt in range(5):            # the pointer is only open for a moment, but retry on Windows
        try:
            os.replace(tmp, pointer)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)
    remove_old(data_dir, prefix)
    return path


def remove_old(data_dir, prefix):
    """Delete databases the pointer no longer names. One still open elsewhere
    (Windows won't delete it) is left for a later start. Loads in progress, and
    partial downloads (*.part, any source), are left alone unless a day old: a
    session that ends mid-download never reaches its own cleanup."""
    if not os.path.isdir(data_dir):
        return
    current = current_path(data_dir)
    for name in os.listdir(data_dir):
        path = os.path.join(data_dir, name)
        try:
            day_old = time.time() - os.path.getmtime(path) > 86400
            if name.endswith(".part"):
                if day_old:
                    os.remove(path)
                continue
            if not name.startswith(prefix + "-") or path == current:
                continue
            if name.endswith(".db") or (name.endswith(".building") and day_old):
                os.remove(path)
        except OSError:
            pass
