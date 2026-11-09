"""Read-only SQLite URIs for local drives and Windows UNC shares."""
import sqlite3


def readonly_uri(path):
    uri=path.as_uri()
    # pathlib emits file://server/share for UNC paths. Default SQLite builds
    # reject a non-local URI authority (SQLITE_ERROR), before touching the DB.
    # Keep the server/share as //server/share in the filename instead.
    if uri.startswith('file://') and not uri.startswith('file:///'):
        uri='file:////'+uri[len('file://'):]
    return uri+'?mode=ro'


def connect_readonly(path):
    # Preserve SQLite locking and read-only/missing-file behavior on SMB.
    return sqlite3.connect(readonly_uri(path),uri=True)
