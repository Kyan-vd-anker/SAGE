import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from . import config

_lock = threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

@contextmanager
def connection():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _add_columns(conn, table, columns):
    """Add missing columns to an existing table (a tiny forward-only migration)."""
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

def init():
    config.DATA.mkdir(parents=True, exist_ok=True)
    config.VAULT.mkdir(parents=True, exist_ok=True)
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    with _lock, connection() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'preference', source TEXT NOT NULL DEFAULT 'user', confidence REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_embeddings(memory_id INTEGER PRIMARY KEY, model TEXT NOT NULL, content_hash TEXT NOT NULL, vector_json TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', status TEXT DEFAULT 'active', priority TEXT DEFAULT 'medium', due_date TEXT, next_action TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, description TEXT DEFAULT '', status TEXT DEFAULT 'todo', priority TEXT DEFAULT 'medium', due_date TEXT, depends_on TEXT DEFAULT '[]', created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, kind TEXT DEFAULT 'planning', status TEXT DEFAULT 'queued', checkpoint TEXT DEFAULT '', result TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS action_log(id INTEGER PRIMARY KEY, plugin TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL, result TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watchlist(id INTEGER PRIMARY KEY, topic TEXT NOT NULL UNIQUE, interval_days INTEGER NOT NULL DEFAULT 1, last_checked TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plugin_requests(id INTEGER PRIMARY KEY, capability TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS focus(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0, project_id INTEGER, goal TEXT NOT NULL DEFAULT '', ends_at TEXT DEFAULT '', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workshop_entries(id INTEGER PRIMARY KEY, project_id INTEGER, note TEXT NOT NULL, measurements TEXT NOT NULL DEFAULT '', image_name TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engineering_assets(id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, extension TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engineering_docs(id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL, body TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'ready', file_path TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engineering_doc_results(id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL, outcome TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', measurements TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(doc_id) REFERENCES engineering_docs(id));
        CREATE TABLE IF NOT EXISTS code_projects(id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT NOT NULL, prompt TEXT NOT NULL, directory TEXT NOT NULL UNIQUE, summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS code_files(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, filename TEXT NOT NULL, relative_path TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES code_projects(id));
        CREATE TABLE IF NOT EXISTS engineering_calculations(id INTEGER PRIMARY KEY, project_id INTEGER, operation TEXT NOT NULL, inputs_json TEXT NOT NULL, result_json TEXT NOT NULL, file_path TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory_items(id INTEGER PRIMARY KEY, project_id INTEGER, part_number TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'each', location TEXT NOT NULL DEFAULT '', supplier TEXT NOT NULL DEFAULT '', reorder_level REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory_movements(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, delta REAL NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(item_id) REFERENCES inventory_items(id));
        CREATE TABLE IF NOT EXISTS design_versions(id INTEGER PRIMARY KEY, project_id INTEGER, asset_id INTEGER, version INTEGER NOT NULL, name TEXT NOT NULL, stored_name TEXT NOT NULL, sha256 TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(asset_id) REFERENCES engineering_assets(id));
        CREATE TABLE IF NOT EXISTS troubleshooting_trees(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, symptom TEXT NOT NULL, steps_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS troubleshooting_sessions(id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL, step_index INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, FOREIGN KEY(tree_id) REFERENCES troubleshooting_trees(id));
        CREATE TABLE IF NOT EXISTS failure_lessons(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, symptom TEXT NOT NULL, cause TEXT NOT NULL DEFAULT '', fix TEXT NOT NULL DEFAULT '', lesson TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tool_approvals(id INTEGER PRIMARY KEY, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, context_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measurement_templates(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', fields_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measurement_runs(id INTEGER PRIMARY KEY, template_id INTEGER NOT NULL, project_id INTEGER, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(template_id) REFERENCES measurement_templates(id));
        CREATE TABLE IF NOT EXISTS measurement_values(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, field_name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', tolerance TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES measurement_runs(id));
        CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, decision TEXT NOT NULL, alternatives TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS session_summaries(id INTEGER PRIMARY KEY, project_id INTEGER, job_id INTEGER, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'drafting', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS computer_tasks(id INTEGER PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', checkpoint TEXT NOT NULL DEFAULT '', steps_json TEXT NOT NULL DEFAULT '[]', pending_json TEXT NOT NULL DEFAULT '', resume_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        ''')
        c.execute("INSERT OR IGNORE INTO focus(id,enabled,project_id,goal,ends_at,updated_at) VALUES(1,0,NULL,'','',?)", (now(),))
        c.executescript('''
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT 'info', data_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'reminder', title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', due_at TEXT NOT NULL, repeat TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', source TEXT NOT NULL DEFAULT 'user', fired_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL, action TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT NOT NULL DEFAULT '', next_run TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT '', builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
        CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status,due_at);
        ''')
        _add_columns(c,'memories',{'importance':'INTEGER NOT NULL DEFAULT 3','pinned':'INTEGER NOT NULL DEFAULT 0','status':"TEXT NOT NULL DEFAULT 'active'",'last_used_at':"TEXT NOT NULL DEFAULT ''",'use_count':'INTEGER NOT NULL DEFAULT 0'})
        _add_columns(c,'tasks',{'sort_order':'INTEGER NOT NULL DEFAULT 0','assignee':"TEXT NOT NULL DEFAULT 'sage'",'kind':"TEXT NOT NULL DEFAULT 'doc'",'estimate_minutes':'INTEGER NOT NULL DEFAULT 0','acceptance':"TEXT NOT NULL DEFAULT ''",'blocked_reason':"TEXT NOT NULL DEFAULT ''",'result_summary':"TEXT NOT NULL DEFAULT ''",'started_at':"TEXT NOT NULL DEFAULT ''",'finished_at':"TEXT NOT NULL DEFAULT ''",'attempts':'INTEGER NOT NULL DEFAULT 0','paused':'INTEGER NOT NULL DEFAULT 0','milestone':"TEXT NOT NULL DEFAULT ''",'needs_input':"TEXT NOT NULL DEFAULT ''",'user_input':"TEXT NOT NULL DEFAULT ''"})
        _add_columns(c,'projects',{'autopilot':'INTEGER NOT NULL DEFAULT 1'})
        _add_columns(c,'tool_approvals',{'computer_task_id':'INTEGER'})
        c.execute('UPDATE tasks SET sort_order=id*10 WHERE sort_order=0')

def rows(sql, args=()):
    with _lock, connection() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def execute(sql, args=()):
    with _lock, connection() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid

def log_action(plugin, args, status, result=''):
    execute('INSERT INTO action_log(plugin,arguments,status,result,created_at) VALUES(?,?,?,?,?)',
            (plugin, json.dumps(args, ensure_ascii=False), status, str(result)[:4000], now()))
