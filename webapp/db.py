"""Database helpers for webapp/main.py.

Contains connection helpers, schema initialization and simple row mapping helpers.
"""
import os
import sqlite3
import time
import shutil
import tempfile
from datetime import datetime
from typing import Dict
from .utils import ROOT

DB_DIR = os.path.join(os.path.dirname(__file__), 'databases')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'testcases.sqlite')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_settings() -> Dict[str, str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT key, value FROM settings')
    rows = cur.fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


def init_db():
    # Create or migrate DB to schema with foreign keys.
    def _create_schema(conn):
        cur = conn.cursor()
        # ensure folders and object_folders exist first so FKs can reference them
        cur.execute('''
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE,
            created_at REAL
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS object_folders (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE,
            created_at REAL
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT,
            created_at REAL
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS testcases (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            tags TEXT,
            filename TEXT,
            data_filename TEXT,
            folder_id TEXT,
            object_folder_id TEXT,
            created_at REAL,
            FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE SET NULL,
            FOREIGN KEY(object_folder_id) REFERENCES object_folders(id) ON DELETE SET NULL
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS suites (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            created_at REAL
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS suite_items (
            id TEXT PRIMARY KEY,
            suite_id TEXT,
            tc_id TEXT,
            position INTEGER,
            FOREIGN KEY(suite_id) REFERENCES suites(id) ON DELETE CASCADE,
            FOREIGN KEY(tc_id) REFERENCES testcases(id) ON DELETE CASCADE
        )
        ''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS profile_kv (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            key TEXT,
            value TEXT,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
        ''')
        conn.commit()

    # Helper to check whether a table has foreign keys defined
    def _has_fks(conn, table_name):
        try:
            cur = conn.cursor()
            cur.execute(f"PRAGMA foreign_key_list({table_name})")
            rows = cur.fetchall()
            return len(rows) > 0
        except Exception:
            return False

    # Open existing DB and decide whether migration is needed
    conn = get_db()
    # enable FK enforcement on this connection
    try:
        conn.execute('PRAGMA foreign_keys = ON')
    except Exception:
        pass
    cur = conn.cursor()

    # If testcases table doesn't exist, just create schema in place
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='testcases'")
    exists = cur.fetchone()
    if not exists:
        _create_schema(conn)
        conn.close()
        return

    # If testcases has FKs already (from previous runs), ensure other tables exist and return
    if _has_fks(conn, 'testcases'):
        # ensure other supporting tables exist (create if missing)
        _create_schema(conn)
        conn.close()
        return

    # At this point, `testcases` exists but without foreign keys.
    # Do not perform an automatic migration here; migrations will be handled manually.
    # Ensure supporting tables/schema exist in-place and continue with non-destructive
    # column checks below.
    _create_schema(conn)
    # If the table already existed before adding data_filename, ensure the column exists
    cur.execute("PRAGMA table_info(testcases)")
    cols = [r[1] for r in cur.fetchall()]
    if 'data_filename' not in cols:
        try:
            cur.execute("ALTER TABLE testcases ADD COLUMN data_filename TEXT")
        except Exception:
            pass
    # ensure folder_id column exists for normalized relation
    if 'folder_id' not in cols:
        try:
            cur.execute("ALTER TABLE testcases ADD COLUMN folder_id TEXT")
        except Exception:
            pass
    # ensure object_folder_id column exists for linking to object_folders
    if 'object_folder_id' not in cols:
        try:
            cur.execute("ALTER TABLE testcases ADD COLUMN object_folder_id TEXT")
        except Exception:
            pass

    # ensure folders table exists
    cur.execute('''
    CREATE TABLE IF NOT EXISTS folders (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        created_at REAL
    )
    ''')
    conn.commit()

    # No legacy folder text migration here; assume existing data was migrated manually if needed.

    cur.execute('''
    CREATE TABLE IF NOT EXISTS suites (
        id TEXT PRIMARY KEY,
        name TEXT,
        description TEXT,
        created_at REAL
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS suite_items (
        id TEXT PRIMARY KEY,
        suite_id TEXT,
        tc_id TEXT,
    position INTEGER,
        FOREIGN KEY(suite_id) REFERENCES suites(id),
        FOREIGN KEY(tc_id) REFERENCES testcases(id)
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS profiles (
        id TEXT PRIMARY KEY,
        name TEXT,
        created_at REAL
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS profile_kv (
        id TEXT PRIMARY KEY,
        profile_id TEXT,
        key TEXT,
        value TEXT,
        FOREIGN KEY(profile_id) REFERENCES profiles(id)
    )
    ''')
    conn.commit()
    # object_folders table to track object repository folders
    cur.execute('''
    CREATE TABLE IF NOT EXISTS object_folders (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        created_at REAL
    )
    ''')
    conn.commit()
    conn.close()


def tc_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'description': row['description'],
        'tags': (row['tags'] or '').split(','),
        'filename': row['filename'],
        'data_filename': row['data_filename'] if 'data_filename' in row.keys() else None,
        'folder': row['folder'] if 'folder' in row.keys() else None,
        'folder_name': row['folder_name'] if 'folder_name' in row.keys() else None,
        'folder_id': row['folder_id'] if 'folder_id' in row.keys() else None,
        'created_at': row['created_at'],
        'object_folder_id': row['object_folder_id'] if 'object_folder_id' in row.keys() else None
    }


def list_folders(conn=None):
    """Return list of folders as dicts {id, name} ordered by name."""
    own = False
    if conn is None:
        conn = get_db()
        own = True
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM folders ORDER BY name')
    out = [{'id': r['id'], 'name': r['name']} for r in cur.fetchall()]
    if own:
        conn.close()
    return out


def create_object_folder(name: str) -> str:
    """Insert a new object folder row and return its uuid."""
    conn = get_db()
    cur = conn.cursor()
    import uuid
    fid = str(uuid.uuid4())
    cur.execute('INSERT INTO object_folders(id, name, created_at) VALUES (?,?,?)', (fid, name, time.time()))
    conn.commit()
    conn.close()
    return fid


def list_object_folders(conn=None):
    """Return list of object folders as dicts {id,name} ordered by name."""
    own = False
    if conn is None:
        conn = get_db()
        own = True
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM object_folders ORDER BY name')
    out = [{'id': r['id'], 'name': r['name']} for r in cur.fetchall()]
    if own:
        conn.close()
    return out


def get_object_folder(folder_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM object_folders WHERE id=?', (folder_id,))
    row = cur.fetchone()
    conn.close()
    return {'id': row['id'], 'name': row['name']} if row else None


__all__ = ['get_db', 'load_settings', 'init_db', 'tc_row_to_dict', 'create_object_folder', 'list_object_folders']
