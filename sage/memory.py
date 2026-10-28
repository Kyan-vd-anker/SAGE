"""Sage's long-term memory.

Saving:    explicit capture ("remember that ..."), the reflection job, tools, task results, daily digest.
Reading:   relevant() blends keyword overlap, optional semantic similarity, importance, recency and pins.
Upkeep:    maintain() merges duplicates, retires stale low-value notes and fills embeddings while Sage is idle.
"""
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db, config

STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'have', 'will', 'your', 'about', 'into', 'would', 'should', 'could',
        'them', 'they', 'then', 'when', 'what', 'user', 'sage', 'how', 'who', 'why', 'you', 'are', 'was', 'has', 'had', 'can', 'not', 'its',
        'our', 'out', 'get', 'got', 'let', 'may', 'new', 'one', 'two', 'use', 'uses', 'via', 'yet', 'but', 'all', 'any', 'she', 'his', 'her',
        'him', 'does', 'did', 'doing', 'been', 'were', 'also', 'just', 'than', 'there', 'their', 'which', 'while', 'where', 'some', 'more'}


def _stem(word):
    """Very small stemmer so 'studies', 'studying' and 'study' match each other."""
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    for suffix in ('ing', 'ed', 'es', 's'):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def keywords(text):
    words = {w.lower() for w in re.findall(r'[\w-]{3,}', text, flags=re.UNICODE)}
    return {_stem(w) for w in words if w not in STOP}


# ------------------------------------------------------------------ saving

def save(content, kind='fact', source='conversation', confidence=0.8, importance=None, status='active', quiet=False):
    content = ' '.join(str(content).split()).strip()
    if not content:
        return None
    if source == 'conversation' and re.search(r'(?i)(query|question|lookup|search request|emotional expression)', kind):
        return None
    if re.search(r'(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret)\s*[:=]\s*\S+', content):
        return None
    existing = db.rows('SELECT id,content,importance,status FROM memories WHERE content=?', (content,))
    if not existing:
        candidate = keywords(content)
        for row in db.rows("SELECT id,content,importance,status FROM memories WHERE status!='archived' ORDER BY updated_at DESC LIMIT 300"):
            prior = keywords(row['content'])
            if len(candidate) >= 3 and len(prior) >= 3 and len(candidate & prior) / min(len(candidate), len(prior)) >= 0.75:
                existing = [row]
                break
    stamp = db.now()
    created = False
    if existing:
        mid = existing[0]['id']
        keep_importance = max(int(existing[0].get('importance') or 3), int(importance or 0)) or 3
        new_status = 'active' if (existing[0].get('status') != 'active' and status == 'active') else existing[0].get('status', 'active')
        db.execute('UPDATE memories SET content=?,kind=?,source=?,confidence=?,importance=?,status=?,updated_at=? WHERE id=?',
                   (content, kind, source, confidence, keep_importance, new_status, stamp, mid))
    else:
        mid = db.execute('INSERT INTO memories(content,kind,source,confidence,importance,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (content, kind, source, confidence, int(importance or 3), status, stamp, stamp))
        created = True
    write_note(mid, content, kind, source, confidence, stamp)
    if created and not quiet and status == 'active':
        try:
            from . import events
            events.publish('memory.saved', 'Remembered', content[:160], 'info', {'id': mid, 'kind': kind})
        except Exception:
            pass
    return mid


def write_note(mid, content, kind, source, confidence, stamp):
    config.VAULT.mkdir(parents=True, exist_ok=True)
    words = keywords(content)
    candidates = []
    for item in db.rows('SELECT id,content FROM memories WHERE id<>? ORDER BY updated_at DESC LIMIT 100', (mid,)):
        shared = words & keywords(item['content'])
        if shared:
            candidates.append((len(shared), item['id']))
    links = [f'[[Memory {other_id}]]' for score, other_id in sorted(candidates, reverse=True)[:5]]
    body = f"# Memory {mid}\n\n{content}\n\n- Kind: {kind}\n- Source: {source}\n- Confidence: {confidence:.2f}\n- Updated: {stamp}\n"
    if links:
        body += '\nRelated memories: ' + ' '.join(links) + '\n'
    (config.VAULT / f'Memory {mid}.md').write_text(body, encoding='utf-8')


_SECRET = re.compile(r'(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret|wachtwoord)\b')
_NOT_FACT_NOUNS = {'question', 'problem', 'issue', 'code', 'computer', 'laptop', 'screen', 'internet', 'wifi', 'battery', 'keyboard', 'mouse', 'app', 'program', 'script', 'server'}
_MY_NOUNS = ('name', 'birthday', 'age', 'favou?rite [a-z ]{2,20}', 'dog', 'cat', 'pet', 'brother', 'sister', 'mother', 'mom', 'father', 'dad', 'partner',
             'girlfriend', 'boyfriend', 'wife', 'husband', 'friend', 'address', 'city', 'hometown', 'major', 'study', 'studies', 'job', 'work', 'car', 'bike',
             'hobby', 'hobbies', 'university', 'school', 'company', 'business', 'room', 'workshop', 'lab', 'printer', 'phone number',
             'naam', 'verjaardag', 'hond', 'kat', 'broer', 'zus', 'moeder', 'vader', 'vriend', 'vriendin', 'stad', 'studie', 'baan', 'werk', 'auto', 'fiets', 'hobby')

_CAPTURE_PATTERNS = [
    # explicit instructions to remember
    (re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:remember|don'?t forget|keep in mind|make a note|note down|note)(?:\s+that)?[:,]?\s+(?P<fact>.{6,300})$", re.I), 'fact', 0.95, 4, None),
    (re.compile(r"^(?:please\s+)?(?:onthoud|onthou|vergeet niet|noteer)(?:\s+(?:dat|even))?[:,]?\s+(?P<fact>.{6,300})$", re.I), 'fact', 0.95, 4, None),
    (re.compile(r"^(?:from now on|voortaan)[, ]+(?P<fact>.{6,300})$", re.I), 'preference', 0.9, 4, 'Preference: '),
    # identity
    (re.compile(r"^(?:my name is|call me|i'?m called|i go by|ik heet|mijn naam is|noem me)\s+(?P<name>[A-Za-zÀ-ÿ'\- ]{2,40}?)\s*[.!]?$", re.I), 'fact', 0.95, 5, 'name'),
    (re.compile(r"^i(?:'m| am)\s+(?P<what>(?:a|an)\s+[A-Za-z][^.!?]{3,100})$", re.I), 'fact', 0.8, 3, 'User is '),
    (re.compile(r"^i\s+(?:live|stay|reside)\s+(?P<what>in\s+[^.!?]{2,80})$", re.I), 'fact', 0.85, 3, 'User lives '),
    (re.compile(r"^ik\s+woon\s+(?P<what>in\s+[^.!?]{2,80})$", re.I), 'fact', 0.85, 3, 'User lives '),
    (re.compile(r"^i(?:'m| am)?\s*(?:currently\s+)?stud(?:y|ying)\s+(?P<what>[^.!?]{3,100})$", re.I), 'fact', 0.85, 3, 'User studies '),
    (re.compile(r"^ik\s+studeer\s+(?P<what>[^.!?]{3,100})$", re.I), 'fact', 0.85, 3, 'User studies '),
    (re.compile(r"^i\s+work\s+(?P<what>(?:at|for|as)\s+[^.!?]{3,100})$", re.I), 'fact', 0.85, 3, 'User works '),
    (re.compile(r"^ik\s+werk\s+(?P<what>(?:bij|als|voor)\s+[^.!?]{3,100})$", re.I), 'fact', 0.85, 3, 'User works '),
    # preferences
    (re.compile(r"^i\s+(?P<verb>prefer|always use|usually use)\s+(?P<what>(?!to ask|a question|help|you\b|some help|a problem|an issue|it\b|this\b|that\b)[^.!?]{3,140})$", re.I), 'preference', 0.82, 4, 'verb'),
    (re.compile(r"^ik\s+(?P<verb>gebruik altijd|gebruik meestal|prefereer)\s+(?P<what>[^.!?]{3,140})$", re.I), 'preference', 0.82, 4, 'nlverb'),
    # my <noun> is <value>
    (re.compile(r"^my\s+(?P<thing>" + '|'.join(_MY_NOUNS) + r")\s+(?:is called|is named|is|are|=)\s+(?P<value>[^.!?]{2,120})$", re.I), 'fact', 0.85, 3, 'my'),
    (re.compile(r"^mijn\s+(?P<thing>" + '|'.join(_MY_NOUNS) + r")\s+(?:is|zijn|heet)\s+(?P<value>[^.!?]{2,120})$", re.I), 'fact', 0.85, 3, 'my'),
]
_VERB_MAP = {'prefer': 'prefers', 'always use': 'always uses', 'usually use': 'usually uses'}
_NL_VERB_MAP = {'gebruik altijd': 'always uses', 'gebruik meestal': 'usually uses', 'prefereer': 'prefers'}


def _sentences(text):
    parts = re.split(r'(?<=[.!?])\s+|\n+|\s+and\s+(?=I\s)|;\s+', text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def capture(text):
    """Save facts the user states outright ("remember that ...", "my dog is called Max", "I study chemistry").
    Returns a list of (memory_id, saved_text). Questions, secrets and obvious one-off chatter are ignored."""
    saved = []
    for sentence in _sentences(text)[:6]:
        if saved or sentence.endswith('?') or _SECRET.search(sentence) or len(sentence) > 320:
            continue
        sentence = sentence.rstrip(' .!')
        for pattern, kind, confidence, importance, style in _CAPTURE_PATTERNS:
            match = pattern.match(sentence)
            if not match:
                continue
            groups = match.groupdict()
            if style is None:
                fact = groups['fact'].strip()
                content = fact[:1].upper() + fact[1:]
            elif style == 'name':
                content = f"The user's name is {groups['name'].strip().title()}"
            elif style == 'verb':
                content = f"User {_VERB_MAP.get(groups['verb'].lower(), groups['verb'].lower())} {groups['what'].strip()}"
            elif style == 'nlverb':
                content = f"User {_NL_VERB_MAP.get(groups['verb'].lower(), groups['verb'].lower())} {groups['what'].strip()}"
            elif style == 'my':
                thing = groups['thing'].strip().lower()
                if thing in _NOT_FACT_NOUNS:
                    continue
                content = f"The user's {thing} is {groups['value'].strip()}"
            else:
                content = style + groups['what'].strip()
            content = content.rstrip(' .!')
            if len(content) < 8:
                continue
            mid = save(content, kind, 'conversation: stated by the user', confidence, importance=importance)
            if mid:
                saved.append((mid, content))
            break
    return saved


_FORGET = re.compile(r"^(?:please\s+)?(?:forget|vergeet|delete the memory|remove the memory|wis de herinnering)(?:\s+(?:that|about|dat|over))?\s+(?P<what>.{3,200})$", re.I)


def forget(text):
    """"Forget that ..." archives the best matching memory (it can be restored from the Planner page). Returns the list archived."""
    match = _FORGET.match(text.strip())
    if not match:
        return None
    wanted = keywords(match.group('what'))
    if not wanted:
        return []
    best = []
    for row in db.rows("SELECT id,content FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 500"):
        overlap = len(wanted & keywords(row['content']))
        if overlap and overlap >= max(1, len(wanted) // 2):
            best.append((overlap, row))
    best.sort(key=lambda item: item[0], reverse=True)
    archived = []
    for _, row in best[:2]:
        db.execute("UPDATE memories SET status='archived',updated_at=? WHERE id=?", (db.now(), row['id']))
        archived.append(row['content'])
    return archived


# ------------------------------------------------------------------ reading

_semantic = {'ok': None, 'at': 0.0}


def semantic_ready():
    """True when the Ollama embedding model is installed. Cached for 5 minutes so chat never waits on it."""
    now = time.time()
    if _semantic['ok'] is not None and now - _semantic['at'] < 300:
        return _semantic['ok']
    ok = False
    try:
        with urllib.request.urlopen(config.OLLAMA_URL.rstrip('/') + '/api/tags', timeout=1.5) as response:
            names = [str(m.get('name', '')) for m in json.loads(response.read()).get('models', [])]
        base = config.EMBED_MODEL.split(':')[0]
        ok = any(n.split(':')[0] == base for n in names)
    except Exception:
        ok = False
    _semantic.update(ok=ok, at=now)
    return ok


def _trip_semantic():
    _semantic.update(ok=False, at=time.time())


def _cosine(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _semantic_scores(query, rows):
    """Similarity of the query to memories that already have a stored embedding (never embeds memories during chat)."""
    if not rows or not semantic_ready():
        return {}
    try:
        qvec = _embed([query[:1000]], timeout=2.5)[0]
    except Exception:
        _trip_semantic()
        return {}
    cached = {r['memory_id']: r for r in db.rows('SELECT memory_id,content_hash,vector_json FROM memory_embeddings WHERE model=?', (config.EMBED_MODEL,))}
    scores = {}
    for row in rows:
        cache = cached.get(row['id'])
        if not cache or cache['content_hash'] != hashlib.sha256(str(row['content'])[:2000].encode('utf-8')).hexdigest():
            continue
        try:
            vector = json.loads(cache['vector_json'])
        except (ValueError, TypeError):
            continue
        if len(vector) == len(qvec):
            scores[row['id']] = _cosine(qvec, vector)
    return scores


def relevant(text, limit=2, track=True):
    """Return only a small set of memories with strong relevance to this specific request."""
    rows = db.rows("SELECT id,content,kind,confidence,importance,pinned,use_count,updated_at FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 800")
    if not rows:
        return []
    now = datetime.now(timezone.utc)
    wanted = keywords(text)
    kw = {r['id']: keywords(r['content']) for r in rows}
    df = {w: sum(1 for k in kw.values() if w in k) for w in wanted}
    idf = {w: math.log(1 + len(rows) / (1 + df[w])) for w in wanted}
    total_idf = sum(idf.values()) or 1.0
    semantic = _semantic_scores(text, rows) if wanted or text.strip() else {}
    scored = []
    for r in rows:
        overlap = wanted & kw[r['id']]
        keyword_score = sum(idf[w] for w in overlap) / total_idf if wanted else 0.0
        if semantic:
            sim = semantic.get(r['id'])
            sem_score = max(0.0, (sim - 0.35) / 0.65) if sim is not None else 0.0
            base = 0.5 * keyword_score + 0.5 * sem_score if sim is not None else keyword_score
        else:
            base = keyword_score
        try:
            age_days = max(0.0, (now - datetime.fromisoformat(r['updated_at'])).total_seconds() / 86400)
        except ValueError:
            age_days = 90.0
        score = base + 0.04 * (int(r['importance'] or 3) - 3) + 0.08 * math.exp(-age_days / 60) + 0.015 * min(int(r['use_count'] or 0), 6)
        scored.append((score, base, r))
    chosen = []
    for score, base, r in sorted(scored, key=lambda x: x[0], reverse=True):
        if len(chosen) >= limit:
            break
        # Pins and importance can rank a matching note higher; they cannot make an unrelated note relevant.
        overlap = len(wanted & kw[r['id']])
        sem_match = semantic.get(r['id'], 0.0) >= 0.68 if semantic else False
        keyword_match = bool(wanted) and overlap >= min(2, len(wanted)) and base >= 0.20
        if r in chosen or not (keyword_match or sem_match):
            continue
        chosen.append({**r, 'relevance_score': round(base, 4)})
    if track and chosen:
        stamp = db.now()
        marks = ','.join('?' for _ in chosen)
        db.execute(f'UPDATE memories SET use_count=use_count+1,last_used_at=? WHERE id IN ({marks})', (stamp, *[r['id'] for r in chosen]))
    return chosen


def _embed(texts, timeout=45):
    request = urllib.request.Request(
        config.OLLAMA_URL.rstrip('/') + '/api/embed',
        data=json.dumps({'model': config.EMBED_MODEL, 'input': texts}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Semantic memory search needs the local Ollama embedding model '{config.EMBED_MODEL}'. Install it with `ollama pull {config.EMBED_MODEL}`. Details: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f'Ollama returned an invalid embedding response: {exc}') from exc
    vectors = payload.get('embeddings') if isinstance(payload, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError('Ollama returned an unexpected number of memory embeddings')
    checked = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise RuntimeError('Ollama returned an invalid memory embedding vector')
        checked.append([float(value) for value in vector])
    return checked


def semantic_search(query, limit=10):
    """Search memories by meaning (used by the search_memory tool). Embeds any memory that has no vector yet."""
    query = str(query).strip()
    if not query:
        return []
    rows = db.rows("SELECT id,content,kind,confidence,updated_at FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 500")
    if not rows:
        return []
    query_vector = _embed([query])[0]
    cached = {row['memory_id']: row for row in db.rows('SELECT memory_id,content_hash,vector_json FROM memory_embeddings WHERE model=?', (config.EMBED_MODEL,))}
    pending = []
    vectors = {}
    for row in rows:
        content = str(row['content'])[:2000]
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        cache = cached.get(row['id'])
        if cache and cache['content_hash'] == digest:
            try:
                vector = json.loads(cache['vector_json'])
            except (ValueError, TypeError):
                vector = None
            if isinstance(vector, list) and len(vector) == len(query_vector) and all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
                vectors[row['id']] = vector
        if row['id'] not in vectors:
            pending.append((row, digest, content))
    for offset in range(0, len(pending), 32):
        batch = pending[offset:offset + 32]
        batch_vectors = _embed([item[2] for item in batch])
        for (row, digest, _), vector in zip(batch, batch_vectors):
            db.execute('INSERT OR REPLACE INTO memory_embeddings(memory_id,model,content_hash,vector_json,updated_at) VALUES(?,?,?,?,?)',
                       (row['id'], config.EMBED_MODEL, digest, json.dumps(vector, separators=(',', ':')), db.now()))
            vectors[row['id']] = vector
    ranked = []
    for row in rows:
        vector = vectors.get(row['id'])
        if vector is None or len(vector) != len(query_vector):
            continue
        ranked.append((_cosine(query_vector, vector), row))
    ranked.sort(key=lambda item: (item[0], item[1]['updated_at']), reverse=True)
    return [{**row, 'similarity': round(score, 4)} for score, row in ranked[:max(0, limit)]]


# ------------------------------------------------------------------ management (API / Planner page)

def list_memories(status='active', query='', limit=200):
    where, args = [], []
    if status and status != 'all':
        where.append('status=?')
        args.append(status)
    if query:
        where.append('content LIKE ?')
        args.append('%' + query + '%')
    sql = 'SELECT id,content,kind,source,confidence,importance,pinned,status,use_count,last_used_at,created_at,updated_at FROM memories'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY pinned DESC, updated_at DESC LIMIT ?'
    return db.rows(sql, (*args, int(limit)))


def update_memory(memory_id, **fields):
    allowed = {'pinned': lambda v: 1 if v else 0, 'importance': lambda v: max(1, min(5, int(v))),
               'status': lambda v: v if v in ('active', 'suggested', 'archived') else 'active',
               'content': lambda v: ' '.join(str(v).split())[:1000]}
    sets, args = [], []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f'{key}=?')
            args.append(allowed[key](value))
    if not sets:
        raise ValueError('Nothing to update')
    rows = db.rows('SELECT id FROM memories WHERE id=?', (int(memory_id),))
    if not rows:
        raise ValueError('Memory not found')
    sets.append('updated_at=?')
    db.execute(f"UPDATE memories SET {','.join(sets)} WHERE id=?", (*args, db.now(), int(memory_id)))
    return db.rows('SELECT * FROM memories WHERE id=?', (int(memory_id),))[0]


# ------------------------------------------------------------------ upkeep

def backfill_embeddings(limit=48):
    """Embed memories that have no vector yet (runs while Sage is idle)."""
    if not semantic_ready():
        return 0
    have = {r['memory_id']: r['content_hash'] for r in db.rows('SELECT memory_id,content_hash FROM memory_embeddings WHERE model=?', (config.EMBED_MODEL,))}
    pending = []
    for row in db.rows("SELECT id,content FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 800"):
        content = str(row['content'])[:2000]
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if have.get(row['id']) != digest:
            pending.append((row['id'], digest, content))
        if len(pending) >= limit:
            break
    done = 0
    for offset in range(0, len(pending), 16):
        batch = pending[offset:offset + 16]
        try:
            vectors = _embed([b[2] for b in batch])
        except RuntimeError:
            _trip_semantic()
            break
        for (mid, digest, _), vector in zip(batch, vectors):
            db.execute('INSERT OR REPLACE INTO memory_embeddings(memory_id,model,content_hash,vector_json,updated_at) VALUES(?,?,?,?,?)',
                       (mid, config.EMBED_MODEL, digest, json.dumps(vector, separators=(',', ':')), db.now()))
            done += 1
    return done


def maintain():
    """Nightly tidy-up. Merges near-duplicates, retires stale low-value memories and fills missing embeddings."""
    rows = db.rows("SELECT id,content,confidence,importance,pinned,use_count,updated_at FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 800")
    keysets = {r['id']: keywords(r['content']) for r in rows}
    archived = set()
    merged = 0
    order = sorted(rows, key=lambda r: (r['pinned'], r['importance'], r['confidence'], r['updated_at']), reverse=True)
    for i, keep in enumerate(order):
        if keep['id'] in archived:
            continue
        for other in order[i + 1:]:
            if other['id'] in archived or other['pinned']:
                continue
            a, b = keysets[keep['id']], keysets[other['id']]
            if len(a) >= 3 and len(b) >= 3 and len(a & b) / min(len(a), len(b)) >= 0.8:
                db.execute("UPDATE memories SET status='archived',updated_at=? WHERE id=?", (db.now(), other['id']))
                archived.add(other['id'])
                merged += 1
    stale = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec='seconds')
    for r in rows:
        if r['id'] in archived or r['pinned']:
            continue
        if int(r['importance'] or 3) <= 2 and not int(r['use_count'] or 0) and r['updated_at'] < cutoff:
            db.execute("UPDATE memories SET status='archived',updated_at=? WHERE id=?", (db.now(), r['id']))
            stale += 1
    embedded = backfill_embeddings()
    summary = f'Merged {merged} duplicate memories, retired {stale} stale ones, embedded {embedded}.'
    if merged or stale:
        try:
            from . import events
            events.publish('memory.maintenance', 'Memory tidied', summary, 'info')
        except Exception:
            pass
    return summary


def digest_day(hours=24):
    """Summarise the last day of chat into one memory (runs as a background job; needs the local model)."""
    from . import llm
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds')
    msgs = db.rows("SELECT role,content FROM messages WHERE created_at>=? AND role IN ('user','assistant') ORDER BY id", (since,))
    user_msgs = [m for m in msgs if m['role'] == 'user']
    if len(user_msgs) < 3:
        return 'Not enough conversation today to summarise.'
    transcript = '\n'.join(f"{'User' if m['role'] == 'user' else 'Sage'}: {m['content'][:400]}" for m in msgs)[-6000:]
    text = llm.chat([
        {'role': 'system', 'content': "Summarise this conversation for long-term memory in at most 5 short bullet points: what the user worked on, decisions made, preferences or facts revealed, and open questions. Use only what is in the text. No greetings, no secrets."},
        {'role': 'user', 'content': transcript}], timeout=180, max_tokens=420, background=True)
    text = ' '.join(text.split())[:900]
    if len(text) < 30:
        return 'The model returned no usable summary.'
    stamp = datetime.now().strftime('%Y-%m-%d')
    mid = save(f'Daily summary {stamp}: {text}', 'daily_summary', 'daily digest', 0.7, importance=2, quiet=True)
    return f'Saved daily summary (memory {mid}).'


def save_project_note(project_id, name, description, task_ids):
    config.VAULT.mkdir(parents=True, exist_ok=True)
    related_words = keywords(name + ' ' + description)
    memory_matches = []
    for item in db.rows("SELECT id,content FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 100"):
        overlap = related_words & keywords(item['content'])
        if overlap:
            memory_matches.append((len(overlap), item['id']))
    links = ' '.join(f'[[Task {task_id}]]' for task_id in task_ids)
    related = ' '.join(f'[[Memory {mid}]]' for _, mid in sorted(memory_matches, reverse=True)[:5])
    body = f"# {name}\n\n{description}\n\n- Source: conversation\n- Created: {db.now()}\n\nTasks: {links}\n\nRelated memories: {related}\n"
    (config.VAULT / f'Project {project_id}.md').write_text(body, encoding='utf-8')
    for task_id in task_ids:
        task = db.rows('SELECT title,description FROM tasks WHERE id=?', (task_id,))
        if task:
            (config.VAULT / f'Task {task_id}.md').write_text(f"# {task[0]['title']}\n\n{task[0]['description']}\n\nProject: [[Project {project_id}]]\n", encoding='utf-8')
