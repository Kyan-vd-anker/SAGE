"""The library: Sage's index of Kyan's own material (Obsidian notes, READMEs, datasheets, source code in his repos).

Memory holds what was said and done. The library holds what was written: the wiring notes in the vault, the README of a project, the
pinout in a datasheet PDF, the function that already solves the problem. Sage can now answer "how did I wire the depth sensor?" from
the file itself.

What is indexed:   SAGE_LIBRARY_ROOTS (folders, separated by ";") and SAGE_REPO_ROOTS (git repositories). Text and code files up to
                   400 KB and PDFs up to 25 MB / 120 pages. Secrets are never indexed: files named like credentials are skipped and
                   lines that assign a password, token or key are dropped.
How it is stored:  chunks (about 1,100 characters, split at headings or function boundaries) in SQLite. An FTS5 table gives instant
                   keyword/BM25 search; chunks are also embedded with the local Ollama embedding model while Sage is idle, so a
                   search by meaning can re-rank (and, with numpy installed, widen) the keyword hits.
Latency:           indexing is a background job that yields to chat. A normal chat turn only touches the library when the message
                   asks about the user's own files, and then it is one FTS query of a few milliseconds, no model call.
Safety:            everything from a file is untrusted text. Prompts label it reference-only and the search tool is on the
                   untrusted-input list, so a README cannot give Sage orders.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config, db, memory

CHUNK_CHARS = 1100
MAX_TEXT_BYTES = 400_000
MAX_PDF_BYTES = 25_000_000
PDF_MAX_PAGES = 120
TEXT_SUFFIXES = {'.md', '.markdown', '.txt', '.rst', '.py', '.js', '.mjs', '.ts', '.tsx', '.jsx', '.html', '.css', '.c', '.h', '.cpp', '.hpp',
                 '.ino', '.java', '.cs', '.go', '.rs', '.sh', '.ps1', '.bat', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.tex', '.sql'}
MARKDOWN_SUFFIXES = {'.md', '.markdown', '.txt', '.rst', '.tex'}
SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv', 'env', 'dist', 'build', '.next', '.obsidian', '.trash',
             '.idea', '.vscode', 'target', '.pytest_cache', '.mypy_cache', 'site-packages'}
_SENSITIVE_NAME = re.compile(r'(?i)(^\.env|\.local\.|(^|[._-])(secrets?|credentials?|passwords?|tokens?|wachtwoord)([._-]|$)|id_rsa|\.pem$|\.key$|\.kdbx$|wallet)')
_SECRET_LINE = re.compile(r'(?i)(api[_-]?key|access[_-]?token|secret|passw(?:or)?d|wachtwoord|private[_-]?key)\s*[:=]\s*\S+')
_BOUNDARY = re.compile(r'^(?:async\s+def |def |class |function |async function |export (?:default )?(?:async )?(?:function|class) |'
                       r'(?:static |void |int |float |double |bool |char |String |unsigned )[\w *&:<>,]+\()')
_LIBRARY_CUE = re.compile(r"(?i)\b(datasheet|data sheet|readme|my notes?|my vault|obsidian|in my (?:repo|repository|code|project files?|notes|vault)|"
                          r"according to my|how did i (?:build|wire|set up|configure|solve|make|do)|pinout|schematic|wiring diagram|which file|"
                          r"in the (?:repo|codebase|source code)|where (?:is|did i (?:put|write|save))|what does my [a-z ]{0,20}(?:code|script|firmware|sketch))\b")

_lock = threading.Lock()
_VEC = {'key': None, 'ids': [], 'matrix': None}


# ------------------------------------------------------------------ what to index

def roots():
    found = []
    for part in re.split(r'[;\n]', f'{config.LIBRARY_ROOTS};{config.REPO_ROOTS}'):
        part = part.strip().strip('"')
        if part and os.path.isdir(part):
            norm = os.path.normcase(os.path.abspath(part))
            if norm not in [os.path.normcase(os.path.abspath(f)) for f in found]:
                found.append(os.path.abspath(part))
    return found


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))]) == os.path.normcase(os.path.abspath(folder))
    except ValueError:
        return False


def _wanted(path):
    name = os.path.basename(path)
    suffix = os.path.splitext(name)[1].lower()
    if _SENSITIVE_NAME.search(name):
        return False
    return suffix in TEXT_SUFFIXES or suffix == '.pdf'


def _candidates(folder):
    """Yield (path, mtime, size) for the indexable files under one root."""
    private = [config.DATA, config.VAULT]
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not any(_inside(os.path.join(current, d), p) for p in private)]
        for name in files:
            path = os.path.join(current, name)
            if not _wanted(path):
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            limit = MAX_PDF_BYTES if name.lower().endswith('.pdf') else MAX_TEXT_BYTES
            if 0 < info.st_size <= limit:
                yield path, info.st_mtime, info.st_size


# ------------------------------------------------------------------ chunking

def _clean(text):
    return '\n'.join(line for line in text.splitlines() if not _SECRET_LINE.search(line))


def chunk_markdown(text):
    chunks, heading, buf, start, size = [], '', [], 1, 0

    def flush():
        nonlocal buf, size
        body = '\n'.join(buf).strip()
        if body:
            chunks.append((heading, start, body))
        buf, size = [], 0

    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r'^(#{1,4})\s+(.*)', line)
        if match:
            flush()
            heading, start = match.group(2).strip()[:100], number
            continue
        if not buf:
            start = number
        buf.append(line)
        size += len(line) + 1
        if (size >= CHUNK_CHARS and not line.strip()) or size >= int(CHUNK_CHARS * 1.5):
            flush()
    flush()
    return chunks


def chunk_code(text):
    chunks, heading, buf, start, size = [], '', [], 1, 0

    def flush():
        nonlocal buf, size
        body = '\n'.join(buf).strip()
        if body:
            chunks.append((heading, start, body))
        buf, size = [], 0

    for number, line in enumerate(text.splitlines(), 1):
        if _BOUNDARY.match(line) and size >= 300:
            flush()
        if _BOUNDARY.match(line):
            heading = line.strip()[:100]
        if not buf:
            start = number
        buf.append(line)
        size += len(line) + 1
        if size >= int(CHUNK_CHARS * 1.4):
            flush()
    flush()
    return chunks


def chunk_pages(pages):
    chunks = []
    for number, page in enumerate(pages, 1):
        buf, size = [], 0
        for paragraph in re.split(r'\n\s*\n|\n(?=[A-Z0-9][^\n]{0,60}\n)', page):
            paragraph = ' '.join(paragraph.split())
            if not paragraph:
                continue
            buf.append(paragraph)
            size += len(paragraph)
            if size >= CHUNK_CHARS:
                chunks.append((f'page {number}', 0, '\n'.join(buf)))
                buf, size = [], 0
        if buf:
            chunks.append((f'page {number}', 0, '\n'.join(buf)))
    return chunks


def chunk_file(path):
    """-> list of (heading, line, text) for one file, or None when it cannot be read."""
    suffix = os.path.splitext(path)[1].lower()
    try:
        if suffix == '.pdf':
            from . import pdftext
            with open(path, 'rb') as handle:
                pages, _meta = pdftext.extract(handle.read(), max_pages=PDF_MAX_PAGES)
            return [(h, n, _clean(t)) for h, n, t in chunk_pages(pages) if _clean(t).strip()]
        with open(path, 'rb') as handle:
            raw = handle.read(MAX_TEXT_BYTES + 1)
    except Exception:
        return None
    if b'\x00' in raw[:2000]:
        return None
    text = _clean(raw.decode('utf-8', errors='replace'))
    return chunk_markdown(text) if suffix in MARKDOWN_SUFFIXES else chunk_code(text)


# ------------------------------------------------------------------ index maintenance

def _has_fts():
    return bool(db.rows("SELECT 1 FROM sqlite_master WHERE name='library_fts'"))


def _drop_file(path):
    ids = [r['id'] for r in db.rows('SELECT id FROM library_chunks WHERE path=?', (path,))]
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ','.join('?' for _ in batch)
        if _has_fts():
            db.execute(f'DELETE FROM library_fts WHERE rowid IN ({marks})', tuple(batch))
        db.execute(f'DELETE FROM library_embeddings WHERE chunk_id IN ({marks})', tuple(batch))
    db.execute('DELETE FROM library_chunks WHERE path=?', (path,))
    db.execute('DELETE FROM library_files WHERE path=?', (path,))


def _store(path, root, mtime, size, chunks):
    _drop_file(path)
    fts = _has_fts()
    for ordinal, (heading, line, text) in enumerate(chunks):
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        cid = db.execute('INSERT INTO library_chunks(path,ordinal,heading,line,text,content_hash) VALUES(?,?,?,?,?,?)',
                         (path, ordinal, heading, line, text, digest))
        if fts:
            db.execute('INSERT INTO library_fts(rowid,heading,text,path,chunk_id) VALUES(?,?,?,?,?)', (cid, heading, text, path, cid))
    db.execute('INSERT OR REPLACE INTO library_files(path,root,mtime,size,chunks,indexed_at) VALUES(?,?,?,?,?,?)',
               (path, root, mtime, size, len(chunks), db.now()))


def _yield_to_chat():
    from . import llm
    if llm._background_interrupted.is_set():
        raise llm.BackgroundInterrupted('Background work yielded to chat.')


def scan(budget=25.0, max_files=80, check_yield=False):
    """Index new and changed files, drop deleted ones. Time-boxed; call again for the rest.
    -> {'indexed': n, 'removed': n, 'pending': files still to do, 'skipped': unreadable files}"""
    started = time.time()
    known = {r['path']: r for r in db.rows('SELECT path,root,mtime,size FROM library_files')}
    seen, todo = set(), []
    live_roots = roots()
    for root in live_roots:
        for path, mtime, size in _candidates(root):
            seen.add(path)
            old = known.get(path)
            if not old or abs(old['mtime'] - mtime) > 0.5 or old['size'] != size:
                todo.append((path, root, mtime, size))
    removed = 0
    for path, row in known.items():
        if path not in seen and any(_inside(path, r) for r in live_roots):
            _drop_file(path)
            removed += 1
        elif not any(_inside(path, r) for r in live_roots):
            _drop_file(path)                                   # its root was removed from the settings
            removed += 1
    todo.sort(key=lambda item: (item[0].lower().endswith('.pdf'), item[0]))   # cheap text files first, PDFs last
    indexed = skipped = 0
    for path, root, mtime, size in todo:
        if indexed >= max_files or time.time() - started > budget:
            break
        if check_yield:
            _yield_to_chat()
        chunks = chunk_file(path)
        if chunks is None:
            skipped += 1
            db.execute('INSERT OR REPLACE INTO library_files(path,root,mtime,size,chunks,indexed_at) VALUES(?,?,?,?,0,?)', (path, root, mtime, size, db.now()))
            continue
        _store(path, root, mtime, size, chunks)
        indexed += 1
    _VEC['key'] = None
    result = {'indexed': indexed, 'removed': removed, 'pending': max(0, len(todo) - indexed - skipped), 'skipped': skipped}
    memory._set_setting('library_last_scan', json.dumps({**result, 'at': db.now()}))
    return result


def embed_pending(limit=64, budget=30.0, check_yield=False):
    """Embed chunks that have no vector yet. -> number embedded (0 when the embedding model is not installed)."""
    if not memory.semantic_ready():
        return 0
    started = time.time()
    rows = db.rows('SELECT c.id,c.heading,c.text,c.path,c.content_hash FROM library_chunks c LEFT JOIN library_embeddings e '
                   'ON e.chunk_id=c.id AND e.model=? AND e.content_hash=c.content_hash WHERE e.chunk_id IS NULL ORDER BY c.id LIMIT ?',
                   (config.EMBED_MODEL, int(limit)))
    done = 0
    for start in range(0, len(rows), 16):
        if time.time() - started > budget:
            break
        if check_yield:
            _yield_to_chat()
        batch = rows[start:start + 16]
        try:
            vectors = memory._embed([f"{os.path.basename(r['path'])} {r['heading']}\n{r['text']}"[:1500] for r in batch], timeout=90)
        except RuntimeError:
            memory._trip_semantic()
            break
        for row, vector in zip(batch, vectors):
            db.execute('INSERT OR REPLACE INTO library_embeddings(chunk_id,model,content_hash,vector_json) VALUES(?,?,?,?)',
                       (row['id'], config.EMBED_MODEL, row['content_hash'], json.dumps(vector, separators=(',', ':'))))
            done += 1
    if done:
        _VEC['key'] = None
    return done


def _missing_embeddings():
    return db.rows('SELECT COUNT(*) AS n FROM library_chunks c LEFT JOIN library_embeddings e ON e.chunk_id=c.id AND e.model=? '
                   'AND e.content_hash=c.content_hash WHERE e.chunk_id IS NULL', (config.EMBED_MODEL,))[0]['n']


def run_job(job=None):
    """The 'library_index' background job (daily schedule, or "reindex library"). Re-queues itself while work remains."""
    if not config.LIBRARY_ENABLED:
        return 'The library is switched off (SAGE_LIBRARY=0).'
    if not roots():
        return 'No library folders are configured. Set SAGE_LIBRARY_ROOTS (notes, datasheets) or SAGE_REPO_ROOTS (git repositories).'
    stats = scan(check_yield=True)
    embedded = embed_pending(check_yield=True)
    remaining = stats['pending'] + (_missing_embeddings() if memory.semantic_ready() else 0)
    if remaining and not db.rows("SELECT id FROM jobs WHERE kind='library_index' AND status IN ('queued','running') AND id<>?", (int((job or {}).get('id') or 0),)):
        from . import jobs
        jobs.create('Index library', 'library', 'library_index')
    return (f"Indexed {stats['indexed']} files, removed {stats['removed']}, skipped {stats['skipped']}; embedded {embedded} chunks"
            f"{'; more to do, continuing when idle' if remaining else ''}.")


def reset():
    """Forget the file list so the next scan re-reads everything."""
    db.execute('DELETE FROM library_files')


# ------------------------------------------------------------------ searching

def _terms(query):
    words, seen = [], set()
    for word in re.findall(r'[\w]{2,}', str(query).lower(), flags=re.UNICODE):
        if word in memory.STOP or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words[:12]


def _fts_ids(terms, count):
    if not terms or not _has_fts():
        return None
    match = ' OR '.join('"' + t.replace('"', '') + '"' for t in terms)
    try:
        rows = db.rows('SELECT rowid AS id FROM library_fts WHERE library_fts MATCH ? ORDER BY bm25(library_fts, 4.0, 1.0) LIMIT ?', (match, count))
    except Exception:
        try:
            rows = db.rows('SELECT rowid AS id FROM library_fts WHERE library_fts MATCH ? LIMIT ?', (match, count))
        except Exception:
            return None
    return [r['id'] for r in rows]


def _scan_ids(terms, count):
    """Fallback without FTS5: score recent chunks by how many query terms they contain."""
    scored = []
    for row in db.rows('SELECT id,heading,text FROM library_chunks ORDER BY id DESC LIMIT 8000'):
        haystack = (row['heading'] + ' ' + row['text']).lower()
        hits = sum(1 for t in terms if t in haystack)
        if hits:
            scored.append((hits, row['id']))
    return [i for _, i in sorted(scored, reverse=True)[:count]]


def _numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def _vector_matrix():
    np = _numpy()
    if np is None:
        return None
    stats_row = db.rows('SELECT COUNT(*) AS n, COALESCE(MAX(chunk_id),0) AS m FROM library_embeddings')[0]
    key = (str(config.DB_PATH), config.EMBED_MODEL, stats_row['n'], stats_row['m'])
    if _VEC['key'] == key and _VEC['matrix'] is not None:
        return _VEC
    ids, vectors = [], []
    for row in db.rows('SELECT chunk_id,vector_json FROM library_embeddings WHERE model=?', (config.EMBED_MODEL,)):
        try:
            vectors.append(json.loads(row['vector_json']))
            ids.append(row['chunk_id'])
        except (ValueError, TypeError):
            continue
    if not vectors:
        return None
    matrix = np.asarray(vectors, dtype='float32')
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    _VEC.update(key=key, ids=ids, matrix=matrix / norms)
    return _VEC


def _semantic_ranking(query, candidate_ids, deep):
    """Chunk ids ordered by similarity to the query: the candidates (always) plus, with numpy and deep=True, the whole library."""
    if not memory.semantic_ready():
        return []
    try:
        qvec = memory._embed([query[:1000]], timeout=10 if deep else 2.5)[0]
    except Exception:
        memory._trip_semantic()
        return []
    np = _numpy()
    if np is not None and deep:
        store = _vector_matrix()
        if store is not None and store['matrix'].shape[1] == len(qvec):
            q = np.asarray(qvec, dtype='float32')
            norm = float(np.linalg.norm(q)) or 1.0
            scores = store['matrix'] @ (q / norm)
            order = np.argsort(-scores)[:20]
            return [store['ids'][int(i)] for i in order if scores[int(i)] >= 0.45]
    if not candidate_ids:
        return []
    marks = ','.join('?' for _ in candidate_ids)
    ranked = []
    for row in db.rows(f'SELECT chunk_id,vector_json FROM library_embeddings WHERE model=? AND chunk_id IN ({marks})', (config.EMBED_MODEL, *candidate_ids)):
        try:
            vector = json.loads(row['vector_json'])
        except (ValueError, TypeError):
            continue
        if len(vector) == len(qvec):
            ranked.append((memory._cosine(qvec, vector), row['chunk_id']))
    return [i for score, i in sorted(ranked, reverse=True) if score >= 0.45]


def search(query, limit=5, semantic=True, deep=False, path_contains='', max_chars=700):
    """Find passages in the user's own files. Keyword (BM25) hits are always used; with the embedding model installed they are fused with
    a semantic ranking. semantic=False never calls a model (the chat path). -> list of {file, section, line, text, path}"""
    terms = _terms(query)
    if not terms:
        return []
    count = max(limit * 8, 40)
    ids = _fts_ids(terms, count)
    if ids is None:
        ids = _scan_ids(terms, count)
    semantic_ids = _semantic_ranking(str(query), ids, deep) if semantic else []
    fused = {}
    for ranking in (ids, semantic_ids):
        for position, cid in enumerate(ranking):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (20 + position)
    needle = path_contains.lower().strip()
    out = []
    for cid in sorted(fused, key=fused.get, reverse=True):
        row = db.rows('SELECT c.id,c.path,c.heading,c.line,c.text,f.root FROM library_chunks c JOIN library_files f ON f.path=c.path WHERE c.id=?', (cid,))
        if not row:
            continue
        r = row[0]
        if needle and needle not in r['path'].lower():
            continue
        try:
            relative = os.path.relpath(r['path'], r['root']).replace('\\', '/')
        except ValueError:
            relative = os.path.basename(r['path'])
        root_name = os.path.basename(r['root'].rstrip('/\\')) or r['root']
        out.append({'file': f'{root_name}/{relative}', 'section': r['heading'], 'line': r['line'],
                    'text': r['text'][:max_chars], 'path': r['path']})
        if len(out) >= limit:
            break
    return out


def wants_library(text):
    return bool(_LIBRARY_CUE.search(text or ''))


def auto_context(text, limit=None):
    """Passages to add to a chat prompt, only when the message asks about the user's own files. No model call, one FTS query."""
    if not config.LIBRARY_ENABLED or not wants_library(text):
        return []
    count = int(limit or config.LIBRARY_AUTO_CHUNKS)
    if count <= 0 or not db.rows('SELECT 1 FROM library_chunks LIMIT 1'):
        return []
    hits = search(text, limit=count, semantic=False, max_chars=600)
    return [{'file': h['file'], 'section': h['section'], 'line': h['line'], 'text': h['text']} for h in hits]


def status():
    last = {}
    try:
        last = json.loads(memory._setting('library_last_scan', '') or '{}')
    except (ValueError, TypeError):
        pass
    files = db.rows('SELECT COUNT(*) AS n FROM library_files WHERE chunks>0')[0]['n']
    chunks = db.rows('SELECT COUNT(*) AS n FROM library_chunks')[0]['n']
    embedded = chunks - _missing_embeddings() if chunks else 0
    return {'enabled': config.LIBRARY_ENABLED, 'roots': roots(), 'files': files, 'chunks': chunks, 'embedded': embedded,
            'fts': _has_fts(), 'numpy': _numpy() is not None, 'last_scan': last}
