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

@contextmanager
def transaction():
    """Run a group of database operations under the shared write lock."""
    with _lock, connection() as conn:
        yield conn

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
        CREATE TABLE IF NOT EXISTS completed_projects(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, completed_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
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
        _add_columns(c, 'messages', {'model': 'TEXT', 'source_docs': 'TEXT'})
        _add_columns(c, 'tool_approvals', {'answer': 'TEXT NOT NULL DEFAULT ""'})
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
        c.execute("INSERT OR IGNORE INTO completed_projects(project_id,name,completed_at) SELECT id,name,created_at FROM projects WHERE status='completed'")
        _add_columns(c,'tool_approvals',{'computer_task_id':'INTEGER'})
        c.execute('UPDATE tasks SET sort_order=id*10 WHERE sort_order=0')
        # Memory tiers, supersede history, knowledge graph and per-project state of play (see memory.py, entities.py, userprofile.py).
        _add_columns(c,'memories',{'tier':"TEXT NOT NULL DEFAULT 'semantic'",'project_id':'INTEGER','valid_from':"TEXT NOT NULL DEFAULT ''",'valid_to':"TEXT NOT NULL DEFAULT ''",'supersedes_id':'INTEGER','superseded_by':'INTEGER','expires_at':"TEXT NOT NULL DEFAULT ''"})
        c.executescript('''
        CREATE TABLE IF NOT EXISTS entities(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'thing', description TEXT NOT NULL DEFAULT '', project_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS entities_name ON entities(lower(name));
        CREATE TABLE IF NOT EXISTS entity_aliases(alias TEXT PRIMARY KEY, entity_id INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS entity_aliases_entity ON entity_aliases(entity_id);
        CREATE TABLE IF NOT EXISTS memory_entities(memory_id INTEGER NOT NULL, entity_id INTEGER NOT NULL, PRIMARY KEY(memory_id, entity_id));
        CREATE INDEX IF NOT EXISTS memory_entities_entity ON memory_entities(entity_id);
        CREATE TABLE IF NOT EXISTS entity_links(src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL DEFAULT 'related', weight REAL NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, PRIMARY KEY(src, dst, rel));
        CREATE TABLE IF NOT EXISTS project_state(project_id INTEGER PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', open_loops TEXT NOT NULL DEFAULT '[]', next_step TEXT NOT NULL DEFAULT '', source_msg_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS memories_status_tier ON memories(status, tier);
        ''')
        if not c.execute("SELECT 1 FROM settings WHERE key='memory_tiers_v1'").fetchone():
            c.execute("UPDATE memories SET tier='procedural' WHERE tier='semantic' AND kind='preference'")
            c.execute("UPDATE memories SET tier='episodic' WHERE tier='semantic' AND kind='daily_summary'")
            c.execute("UPDATE memories SET valid_from=created_at WHERE valid_from=''")
            c.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('memory_tiers_v1','1',?)", (now(),))
        # Events as memories (chronicle.py), the indexed library (library.py), and answer feedback and evals (evals.py).
        _add_columns(c,'memories',{'feedback_score':'INTEGER NOT NULL DEFAULT 0'})
        c.executescript('''
        CREATE TABLE IF NOT EXISTS chronicle_queue(id INTEGER PRIMARY KEY, source TEXT NOT NULL, row_id INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chronicle_seen(key TEXT PRIMARY KEY, memory_id INTEGER, created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS chronicle_workshop AFTER INSERT ON workshop_entries BEGIN
            INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('workshop',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END;
        CREATE TRIGGER IF NOT EXISTS chronicle_decision AFTER INSERT ON decision_log BEGIN
            INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('decision',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END;
        CREATE TRIGGER IF NOT EXISTS chronicle_failure AFTER INSERT ON failure_lessons BEGIN
            INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('failure',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END;
        CREATE TRIGGER IF NOT EXISTS chronicle_measurement AFTER INSERT ON measurement_runs BEGIN
            INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('measurement',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END;
        CREATE TRIGGER IF NOT EXISTS chronicle_task_done AFTER UPDATE OF status ON tasks WHEN NEW.status='done' AND OLD.status IS NOT 'done' BEGIN
            INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('task',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END;
        CREATE TABLE IF NOT EXISTS library_files(path TEXT PRIMARY KEY, root TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL, chunks INTEGER NOT NULL DEFAULT 0, indexed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS library_chunks(id INTEGER PRIMARY KEY, path TEXT NOT NULL, ordinal INTEGER NOT NULL, heading TEXT NOT NULL DEFAULT '', line INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, content_hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS library_chunks_path ON library_chunks(path);
        CREATE TABLE IF NOT EXISTS library_embeddings(chunk_id INTEGER PRIMARY KEY, model TEXT NOT NULL, content_hash TEXT NOT NULL, vector_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS answer_trace(message_id INTEGER PRIMARY KEY, user_text TEXT NOT NULL DEFAULT '', memory_ids TEXT NOT NULL DEFAULT '[]', library_refs TEXT NOT NULL DEFAULT '[]', entity_ids TEXT NOT NULL DEFAULT '[]', tools TEXT NOT NULL DEFAULT '[]', model TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'chat', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, rating INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS feedback_message ON feedback(message_id);
        CREATE TABLE IF NOT EXISTS eval_runs(id INTEGER PRIMARY KEY, suite TEXT NOT NULL, summary_json TEXT NOT NULL, results_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS openscad_designs(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS openscad_versions(id INTEGER PRIMARY KEY, design_id INTEGER NOT NULL, version INTEGER NOT NULL, prompt TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', mesh_name TEXT NOT NULL DEFAULT '', render_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(design_id,version), FOREIGN KEY(design_id) REFERENCES openscad_designs(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS openscad_versions_design ON openscad_versions(design_id,version DESC);
        CREATE TABLE IF NOT EXISTS openscad_drawings(id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL UNIQUE, svg TEXT NOT NULL DEFAULT '', dimensions_json TEXT NOT NULL DEFAULT '{}', render_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(version_id) REFERENCES openscad_versions(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS schematic_designs(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schematic_versions(id INTEGER PRIMARY KEY, design_id INTEGER NOT NULL, version INTEGER NOT NULL, prompt TEXT NOT NULL DEFAULT '', spec_json TEXT NOT NULL, svg TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(design_id,version), FOREIGN KEY(design_id) REFERENCES schematic_designs(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS schematic_versions_design ON schematic_versions(design_id,version DESC);
        ''')
        try:
            c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(heading, text, path UNINDEXED, chunk_id UNINDEXED)')
        except sqlite3.OperationalError:
            pass                       # this SQLite build has no FTS5: library.py falls back to a slower keyword scan
        c.execute("CREATE TRIGGER IF NOT EXISTS chronicle_test AFTER INSERT ON engineering_doc_results BEGIN INSERT INTO chronicle_queue(source,row_id,created_at) VALUES('test',NEW.id,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')); END")

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
