"SQLite storage: schema, migrations, and queries."

import sqlite3
from dataclasses import dataclass
from pathlib import Path

def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database. Read-write opens create and migrate; read-only never writes."""
    path = Path(path)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
    
 

