#!/usr/bin/env python3
"""
One-time utility: clear any stale gnclock rows left behind by crashed
piecash/GnuCash sessions.

GnuCash SQLite books track an open write-session using a special
`gnclock` table (hostname + pid). If a script or the GnuCash desktop app
crashes, or is killed, or raises an unhandled exception before calling
book.close(), that lock row is never cleared -- and every subsequent
attempt to open the file for writing will report it as "locked", even
though nothing is actually using it anymore.

Only run this when you are SURE no other process (GnuCash desktop app,
another instance of this importer, etc.) currently has the file open --
clearing a lock while something is genuinely still writing to the file
can cause data corruption.

Usage:
    python clear_gnclock.py [path-to-file.gnucash]

Defaults to "portfolio-sqlite.gnucash" in the current directory if no
path is given.
"""
import sqlite3
import sys


def clear_gnclock(sqlite_path: str):
    conn = sqlite3.connect(sqlite_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM gnclock")
    count = cur.fetchone()[0]
    if count == 0:
        print("No lock rows found - file is already unlocked.")
    else:
        cur.execute("SELECT hostname, pid FROM gnclock")
        for hostname, pid in cur.fetchall():
            print(f"  Found stale lock: host={hostname} pid={pid}")
        cur.execute("DELETE FROM gnclock")
        conn.commit()
        print(f"Cleared {count} stale lock row(s).")
    conn.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "portfolio-sqlite.gnucash"
    clear_gnclock(path)
