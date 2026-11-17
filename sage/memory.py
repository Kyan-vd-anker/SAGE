"""Sage's long-term memory.

Saving:    explicit capture ("remember that ..."), the idle-time LLM capture job, tools, task results, daily digest.
Reading:   relevant() blends keyword overlap, optional semantic similarity, entity links, importance, recency and pins.
           recall() is the deep, slower search behind the search_memory tool.
Upkeep:    maintain() merges duplicates, retires stale low-value notes, links notes to entities and fills embeddings while idle.

Tiers (the `tier` column) decide how a memory is used:
    core        standing facts and rules about the user; compiled into the always-on profile block (userprofile.py)
    working     the current state of something in progress; expires after two weeks unless it is kept alive
    episodic    things that happened (daily summaries, decisions); only retrieved when the question is about the past
    semantic    general facts (the default)
    procedural  how the user likes things done (preferences, routines)

Nothing is overwritten. When a new note updates an older one, the older one becomes `superseded` (it keeps its text, the time it
stopped being true and a pointer to the note that replaced it), so "what did I use before?" can still be answered.
"""
import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db, config, entities

STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'have', 'will', 'your', 'about', 'into', 'would', 'should', 'could',
        'them', 'they', 'then', 'when', 'what', 'user', 'sage', 'how', 'who', 'why', 'you', 'are', 'was', 'has', 'had', 'can', 'not', 'its',
        'our', 'out', 'get', 'got', 'let', 'may', 'new', 'one', 'two', 'use', 'uses', 'via', 'yet', 'but', 'all', 'any', 'she', 'his', 'her',
        'him', 'does', 'did', 'doing', 'been', 'were', 'also', 'just', 'than', 'there', 'their', 'which', 'while', 'where', 'some', 'more'}

TIERS = ('core', 'working', 'episodic', 'semantic', 'procedural')
_TIER_BY_KIND = (('daily_summary', 'episodic'), ('decision', 'episodic'), ('event', 'episodic'),
                 ('preference', 'procedural'), ('routine', 'procedural'), ('habit', 'procedural'), ('skill', 'procedural'))
_TIME_CUE = re.compile(r"(?i)\b(yesterday|last (?:week|month|night|time|year)|earlier|before|previously|ago|when did|what did (?:we|i)|did (?:we|i)|"
                       r"remember when|gisteren|vorige|eerder|laatst|toen)\b")
WORKING_DAYS = 14


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


def infer_tier(kind, content=''):
    kind = str(kind or '').lower()
    for key, tier in _TIER_BY_KIND:
        if key in kind:
            return tier
    return 'semantic'


def _setting(key, default=''):
    rows = db.rows('SELECT value FROM settings WHERE key=?', (key,))
    return rows[0]['value'] if rows else default


def _set_setting(key, value):
    db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', (key, str(value), db.now()))


# ------------------------------------------------------------------ the in-memory keyword index

_IDX = {'key': None, 'rows': []}
_idx_lock = threading.Lock()


def _touch():
    """Forget the cached index (the next read rebuilds it)."""
    with _idx_lock:
        _IDX['key'] = None


def _fingerprint():
    row = db.rows("SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS i, COALESCE(MAX(updated_at),'') AS u, COALESCE(SUM(pinned),0) AS p "
                  "FROM memories WHERE status='active'")[0]
    return (str(config.DB_PATH), row['n'], row['i'], row['u'], row['p'])


def _index():
    """Active memories with their keyword sets, rebuilt only when the table changed (one cheap query tells)."""
    key = _fingerprint()
    with _idx_lock:
        if _IDX['key'] == key:
            return _IDX['rows']
    rows = db.rows("SELECT id,content,kind,tier,confidence,importance,pinned,use_count,updated_at,project_id,expires_at,feedback_score "
                   "FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 800")
    for row in rows:
        row['kw'] = keywords(row['content'])
    with _idx_lock:
        _IDX.update(key=key, rows=rows)
    return rows


# ------------------------------------------------------------------ saving

def _find_near(content, candidate):
    """The stored note that `content` is a near-duplicate of, as (row, relation). relation is 'covered' when the stored note already
    says everything the new one does, otherwise 'changed' (the new one adds or alters something)."""
    if len(candidate) < 3:
        return None
    for row in db.rows("SELECT id,content,importance,status,pinned,tier FROM memories WHERE status NOT IN ('archived','superseded') "
                       "ORDER BY updated_at DESC LIMIT 300"):
        prior = keywords(row['content'])
        if len(prior) >= 3 and len(candidate & prior) / min(len(candidate), len(prior)) >= 0.75:
            return row, ('covered' if candidate <= prior else 'changed')
    return None


def _supersede(old_id, new_id, stamp):
    """Retire a note without losing it: it keeps its text, gains an end time and points at its replacement."""
    db.execute("UPDATE memories SET status='superseded',valid_to=?,superseded_by=?,updated_at=? WHERE id=? AND status='active'",
               (stamp, new_id, stamp, old_id))
    db.execute('INSERT OR IGNORE INTO memory_entities(memory_id,entity_id) SELECT ?,entity_id FROM memory_entities WHERE memory_id=?',
               (new_id, old_id))
    row = db.rows('SELECT content,kind,source,confidence,tier FROM memories WHERE id=?', (old_id,))
    if row:
        write_note(old_id, row[0]['content'], row[0]['kind'], row[0]['source'], row[0]['confidence'], stamp,
                   tier=row[0]['tier'], status='superseded', superseded_by=new_id)


def save(content, kind='fact', source='conversation', confidence=0.8, importance=None, status='active', quiet=False,
         tier=None, project_id=None, entity_names=None, supersedes=None, expires_days=None, distinct=False):
    """Save a memory. Returns its id (a near-duplicate returns the id of the note it matched).

    tier          one of TIERS (default: from `kind`)
    entity_names  names (or {name, kind, description, aliases} dicts) of the entities this note is about; known entities mentioned
                  in the text are linked automatically
    supersedes    id of an active memory this one replaces; with status='suggested' it only proposes the change until approved
    distinct      this is a separate event, never an update of a similar older note (used for logged events: two prints of the
                  same file on different days are two memories, not one superseding the other)
    """
    content = ' '.join(str(content).split()).strip()
    if not content:
        return None
    if source == 'conversation' and re.search(r'(?i)(query|question|lookup|search request|emotional expression)', kind):
        return None
    if re.search(r'(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret)\s*[:=]\s*\S+', content):
        return None
    tier = tier if tier in TIERS else None
    stamp = db.now()
    mid, created, old = None, False, None
    if supersedes:
        rows = db.rows("SELECT id,content,importance,pinned,tier,status FROM memories WHERE id=? AND status='active'", (int(supersedes),))
        old = rows[0] if rows else None
    exact = db.rows("SELECT id,importance,status,tier FROM memories WHERE content=? AND status!='superseded' ORDER BY id DESC LIMIT 1", (content,))
    if exact:
        row = exact[0]
        keep_importance = max(int(row.get('importance') or 3), int(importance or 0)) or 3
        new_status = 'active' if (row['status'] != 'active' and status == 'active') else row['status']
        db.execute('UPDATE memories SET kind=?,source=?,confidence=?,importance=?,status=?,tier=?,updated_at=? WHERE id=?',
                   (kind, source, confidence, keep_importance, new_status, tier or row['tier'], stamp, row['id']))
        mid = row['id']
    else:
        if old is None and not distinct:
            near = _find_near(content, keywords(content))
            if near:
                row, relation = near
                if relation == 'covered':
                    # The stored note already says this (and probably more): keep its wording, only refresh it.
                    new_status = 'active' if (row['status'] == 'suggested' and status == 'active') else row['status']
                    db.execute('UPDATE memories SET importance=?,status=?,updated_at=? WHERE id=?',
                               (max(int(row.get('importance') or 3), int(importance or 0)) or 3, new_status, stamp, row['id']))
                    mid = row['id']
                elif row['status'] == 'suggested':
                    # A suggestion nobody has approved yet can simply be reworded.
                    new_status = 'active' if status == 'active' else 'suggested'
                    db.execute('UPDATE memories SET content=?,kind=?,source=?,confidence=?,importance=?,status=?,updated_at=? WHERE id=?',
                               (content, kind, source, confidence, max(int(row.get('importance') or 3), int(importance or 0)) or 3,
                                new_status, stamp, row['id']))
                    mid = row['id']
                else:
                    old = row
        if mid is None:
            chosen_tier = tier or (old['tier'] if old else None) or infer_tier(kind, content)
            chosen_importance = max(int(importance or 3), int(old['importance'] or 3) if old else 0)
            pinned = int(old['pinned'] or 0) if (old and status == 'active') else 0
            days = expires_days or (WORKING_DAYS if chosen_tier == 'working' else 0)
            expires = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat(timespec='seconds') if days else ''
            mid = db.execute('INSERT INTO memories(content,kind,source,confidence,importance,status,created_at,updated_at,tier,project_id,'
                             'valid_from,supersedes_id,expires_at,pinned) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (content, kind, source, confidence, chosen_importance, status, stamp, stamp, chosen_tier, project_id,
                              stamp, old['id'] if old else None, expires, pinned))
            created = True
            if old and status == 'active':
                _supersede(old['id'], mid, stamp)
    final = db.rows('SELECT content,kind,source,confidence,tier,status,superseded_by FROM memories WHERE id=?', (mid,))[0]
    write_note(mid, final['content'], final['kind'], final['source'], final['confidence'], stamp, tier=final['tier'],
               status=final['status'], superseded_by=final['superseded_by'])
    _link_entities(mid, final['content'], entity_names)
    _touch()
    if created and not quiet and status == 'active':
        try:
            from . import events
            events.publish('memory.saved', 'Remembered' if not old else 'Remembered (updated)', content[:160], 'info', {'id': mid, 'kind': kind})
        except Exception:
            pass
    return mid


def _link_entities(mid, content, entity_names=None):
    try:
        ids = list(entities.find(content))
        for item in entity_names or []:
            spec = item if isinstance(item, dict) else {'name': item}
            eid = entities.ensure(spec.get('name', ''), spec.get('kind', 'thing'), spec.get('description', ''), spec.get('aliases') or ())
            if eid and eid not in ids:
                ids.append(eid)
        entities.link_memory(mid, ids)
    except Exception:
        pass


def write_note(mid, content, kind, source, confidence, stamp, tier='semantic', status='active', superseded_by=None):
    config.VAULT.mkdir(parents=True, exist_ok=True)
    words = keywords(content)
    candidates = []
    for item in db.rows('SELECT id,content FROM memories WHERE id<>? ORDER BY updated_at DESC LIMIT 100', (mid,)):
        shared = words & keywords(item['content'])
        if shared:
            candidates.append((len(shared), item['id']))
    links = [f'[[Memory {other_id}]]' for score, other_id in sorted(candidates, reverse=True)[:5]]
    body = f"# Memory {mid}\n\n{content}\n\n- Kind: {kind}\n- Tier: {tier}\n- Source: {source}\n- Confidence: {confidence:.2f}\n- Updated: {stamp}\n"
    if status != 'active':
        body += f'- Status: {status}\n'
    if superseded_by:
        body += f'- Replaced by: [[Memory {superseded_by}]]\n'
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
_CORE_STYLES = ('name', 'Preference: ', 'User is ', 'User lives ', 'User studies ', 'User works ')    # identity and standing rules


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
            mid = save(content, kind, 'conversation: stated by the user', confidence, importance=importance,
                       tier='core' if style in _CORE_STYLES else None)
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
    if archived:
        _touch()
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


def _entity_memory_ids(entity_ids):
    ids = [int(i) for i in (entity_ids or [])]
    if not ids:
        return set()
    marks = ','.join('?' for _ in ids)
    return {r['memory_id'] for r in db.rows(f'SELECT memory_id FROM memory_entities WHERE entity_id IN ({marks})', tuple(ids))}


# ------------------------------------------------------------------ retrieval weights

DEFAULT_WEIGHTS = {'entity': 0.30, 'sem_mix': 0.5, 'importance': 0.04, 'recency': 0.08, 'use': 0.015, 'feedback': 0.03}
_W = {'at': 0.0, 'db': None, 'value': dict(DEFAULT_WEIGHTS), 'override': None}


def weights():
    """The ranking weights used by relevant(): the defaults, plus any tuned values stored in the settings table (key
    'retrieval_weights', written by evals.tune). Cached for a minute."""
    if _W['override'] is not None:
        return _W['override']
    now = time.time()
    if _W['db'] == str(config.DB_PATH) and now - _W['at'] < 60:
        return _W['value']
    value = dict(DEFAULT_WEIGHTS)
    try:
        for key, item in json.loads(_setting('retrieval_weights', '') or '{}').items():
            if key in value and isinstance(item, (int, float)) and math.isfinite(item):
                value[key] = float(item)
    except (ValueError, TypeError, AttributeError):
        pass
    _W.update(at=now, db=str(config.DB_PATH), value=value)
    return value


def set_weights(values):
    """Store tuned weights (unknown keys are ignored) and apply them straight away."""
    clean = {k: round(float(v), 4) for k, v in (values or {}).items() if k in DEFAULT_WEIGHTS}
    _set_setting('retrieval_weights', json.dumps(clean))
    _W['at'] = 0.0
    return clean


def relevant(text, limit=2, track=True, semantic=True, entity_ids=None, exclude_ids=(), allow_episodic=None):
    """Return only a small set of memories with strong relevance to this specific request.

    semantic=False skips the embedding call (the voice path uses this: keywords and entity links only).
    entity_ids are entities found in the message; memories linked to them qualify even without a keyword match.
    exclude_ids are memories already in the prompt (the core profile). Episodic notes (daily summaries, decisions) are only
    considered when the text asks about the past, unless allow_episodic says otherwise."""
    rows = _index()
    if not rows:
        return []
    if allow_episodic is None:
        allow_episodic = bool(_TIME_CUE.search(text or ''))
    skip = set(exclude_ids or ())
    stamp = db.now()
    rows = [r for r in rows if r['id'] not in skip and (allow_episodic or r['tier'] != 'episodic')
            and not (r['expires_at'] and r['expires_at'] < stamp)]
    if not rows:
        return []
    now = datetime.now(timezone.utc)
    wanted = keywords(text)
    kw = {r['id']: r['kw'] for r in rows}
    df = {w: sum(1 for k in kw.values() if w in k) for w in wanted}
    idf = {w: math.log(1 + len(rows) / (1 + df[w])) for w in wanted}
    total_idf = sum(idf.values()) or 1.0
    linked = _entity_memory_ids(entity_ids)
    semantic_scores = _semantic_scores(text, rows) if (semantic and (wanted or text.strip())) else {}
    scored = []
    w = weights()
    for r in rows:
        overlap = wanted & kw[r['id']]
        keyword_score = sum(idf[w_] for w_ in overlap) / total_idf if wanted else 0.0
        if semantic_scores:
            sim = semantic_scores.get(r['id'])
            sem_score = max(0.0, (sim - 0.35) / 0.65) if sim is not None else 0.0
            base = (1 - w['sem_mix']) * keyword_score + w['sem_mix'] * sem_score if sim is not None else keyword_score
        else:
            base = keyword_score
        try:
            age_days = max(0.0, (now - datetime.fromisoformat(r['updated_at'])).total_seconds() / 86400)
        except ValueError:
            age_days = 90.0
        entity_bonus = w['entity'] if r['id'] in linked else 0.0
        score = (base + entity_bonus + w['importance'] * (int(r['importance'] or 3) - 3) + w['recency'] * math.exp(-age_days / 60)
                 + w['use'] * min(int(r['use_count'] or 0), 6) + w['feedback'] * max(-3, min(3, int(r.get('feedback_score') or 0))))
        scored.append((score, base, r))
    chosen = []
    for score, base, r in sorted(scored, key=lambda x: x[0], reverse=True):
        if len(chosen) >= limit:
            break
        # Pins and importance can rank a matching note higher; they cannot make an unrelated note relevant.
        overlap = len(wanted & kw[r['id']])
        sem_match = semantic_scores.get(r['id'], 0.0) >= 0.68 if semantic_scores else False
        keyword_match = bool(wanted) and overlap >= min(2, len(wanted)) and base >= 0.20
        entity_match = r['id'] in linked
        if not (keyword_match or sem_match or entity_match):
            continue
        chosen.append({**{k: v for k, v in r.items() if k != 'kw'}, 'relevance_score': round(base, 4)})
    if track and chosen:
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
    """Search memories by meaning. Embeds any memory that has no vector yet."""
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


def recall(query, limit=8, tier=None, entity=None, include_history=False, days=None):
    """Deep recall for the search_memory tool: keyword, entity and (when the embedding model is installed) semantic matches fused
    into one ranking, plus matching past conversation, recorded decisions and what Sage knows about the entities mentioned.
    The result is compact on purpose: a small local model has to read all of it."""
    query = ' '.join(str(query or '').split())[:500]
    out = {'query': query, 'memories': [], 'history': [], 'decisions': [], 'entities': []}
    if not query:
        return out
    limit = max(1, min(int(limit or 8), 15))
    entity_ids = list(entities.find(query))
    if entity:
        found = entities.resolve(entity)
        if found and found not in entity_ids:
            entity_ids.insert(0, found)
    wanted = keywords(query)
    cutoff = ''
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec='seconds')
    sql = "SELECT id,content,kind,tier,status,importance,pinned,updated_at,superseded_by FROM memories WHERE status IN " + \
          ("('active','superseded')" if include_history else "('active')")
    args = []
    if tier in TIERS:
        sql += ' AND tier=?'
        args.append(tier)
    if cutoff:
        sql += ' AND updated_at>=?'
        args.append(cutoff)
    rows = db.rows(sql + ' ORDER BY updated_at DESC LIMIT 1500', tuple(args))
    by_id = {r['id']: r for r in rows}
    keyword_rank = []
    if wanted:
        scored = []
        for r in rows:
            overlap = len(wanted & keywords(r['content']))
            if overlap:
                scored.append((overlap / len(wanted) + 0.02 * (int(r['importance'] or 3) - 3) + (0.05 if r['pinned'] else 0), r['id']))
        keyword_rank = [i for _, i in sorted(scored, reverse=True)[:limit * 2]]
    entity_rank = sorted(_entity_memory_ids(entity_ids) & set(by_id), key=lambda i: (by_id[i]['importance'], by_id[i]['updated_at']), reverse=True)[:limit * 2]
    semantic_rank = []
    if semantic_ready():
        try:
            for hit in semantic_search(query, limit=limit * 2):
                if hit['id'] in by_id and hit.get('similarity', 0) >= 0.45:
                    semantic_rank.append(hit['id'])
        except RuntimeError:
            _trip_semantic()
    fused = {}
    for ranking in (keyword_rank, semantic_rank, entity_rank):
        for position, mid in enumerate(ranking):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (20 + position)
    for mid in sorted(fused, key=fused.get, reverse=True)[:limit]:
        row = by_id[mid]
        item = {'id': mid, 'text': row['content'][:300], 'tier': row['tier'], 'saved': row['updated_at'][:10]}
        if row['status'] == 'superseded':
            item['status'] = 'replaced by a newer note'
            item['replaced_by'] = row['superseded_by']
        elif include_history:
            earlier = db.rows('SELECT content,updated_at FROM memories WHERE superseded_by=? ORDER BY id DESC LIMIT 2', (mid,))
            if earlier:
                item['earlier'] = [{'text': e['content'][:200], 'until': e['updated_at'][:10]} for e in earlier]
        out['memories'].append(item)
    if wanted:
        hits = []
        for row in db.rows("SELECT id,role,content,created_at FROM messages WHERE role IN ('user','assistant') AND (source_docs IS NULL OR source_docs='') "
                           "ORDER BY id DESC LIMIT 3000"):
            if cutoff and row['created_at'] < cutoff:
                continue
            overlap = len(wanted & keywords(row['content']))
            if overlap >= min(2, len(wanted)):
                hits.append((overlap, row['id'], row))
        for _, _, row in sorted(hits, key=lambda x: (x[0], x[1]), reverse=True)[:3]:
            out['history'].append({'when': row['created_at'][:16].replace('T', ' '), 'who': row['role'], 'text': row['content'][:240]})
        terms = sorted(wanted, key=len, reverse=True)[:4]
        if terms:
            clause = ' OR '.join('(lower(title) LIKE ? OR lower(decision) LIKE ?)' for _ in terms)
            like = []
            for term in terms:
                like += [f'%{term}%', f'%{term}%']
            for d in db.rows(f'SELECT title,decision,rationale,created_at FROM decision_log WHERE {clause} ORDER BY id DESC LIMIT 3', tuple(like)):
                out['decisions'].append({'when': d['created_at'][:10], 'title': d['title'][:120], 'decision': d['decision'][:240], 'why': (d['rationale'] or '')[:200]})
    out['entities'] = entities.cards_for_ids(entity_ids, limit=2, max_facts=4)
    if not (out['memories'] or out['history'] or out['decisions'] or out['entities']):
        out['note'] = 'Nothing matching was found in saved memory, past conversation or recorded decisions.'
    return out


# ------------------------------------------------------------------ management (API / Planner page)

def list_memories(status='active', query='', limit=200):
    where, args = [], []
    if status and status != 'all':
        where.append('status=?')
        args.append(status)
    if query:
        where.append('content LIKE ?')
        args.append('%' + query + '%')
    sql = ('SELECT id,content,kind,tier,source,confidence,importance,pinned,status,use_count,last_used_at,created_at,updated_at,'
           'supersedes_id,superseded_by FROM memories')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY pinned DESC, updated_at DESC LIMIT ?'
    return db.rows(sql, (*args, int(limit)))


def tier_counts():
    """{'core': 3, ...} for active memories, plus 'suggested' and 'superseded' totals."""
    counts = {t: 0 for t in TIERS}
    for row in db.rows("SELECT tier,COUNT(*) AS n FROM memories WHERE status='active' GROUP BY tier"):
        counts[row['tier']] = row['n']
    for status in ('suggested', 'superseded'):
        counts[status] = db.rows('SELECT COUNT(*) AS n FROM memories WHERE status=?', (status,))[0]['n']
    return counts


def update_memory(memory_id, **fields):
    allowed = {'pinned': lambda v: 1 if v else 0, 'importance': lambda v: max(1, min(5, int(v))),
               'status': lambda v: v if v in ('active', 'suggested', 'archived', 'superseded') else 'active',
               'tier': lambda v: v if v in TIERS else 'semantic',
               'content': lambda v: ' '.join(str(v).split())[:1000]}
    sets, args = [], []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f'{key}=?')
            args.append(allowed[key](value))
    if not sets:
        raise ValueError('Nothing to update')
    rows = db.rows('SELECT id,supersedes_id FROM memories WHERE id=?', (int(memory_id),))
    if not rows:
        raise ValueError('Memory not found')
    stamp = db.now()
    sets.append('updated_at=?')
    db.execute(f"UPDATE memories SET {','.join(sets)} WHERE id=?", (*args, stamp, int(memory_id)))
    if fields.get('status') == 'active' and rows[0]['supersedes_id']:
        # Approving a suggestion that proposed to replace an older note retires the older note.
        previous = db.rows('SELECT status FROM memories WHERE id=?', (rows[0]['supersedes_id'],))
        if previous and previous[0]['status'] == 'active':
            _supersede(rows[0]['supersedes_id'], int(memory_id), stamp)
    _touch()
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


def _queue_backfill():
    """Ask the idle worker to give older memories tiers, entities and relations (a few at a time, see capture.run_backfill)."""
    if not config.CAPTURE_ENABLED:
        return
    cursor = int(_setting('memory_backfill_cursor', '0') or 0)
    if db.rows("SELECT id FROM memories WHERE status='active' AND id>? LIMIT 1", (cursor,)) and \
            not db.rows("SELECT id FROM jobs WHERE kind='memory_backfill' AND status IN ('queued','running')"):
        from . import jobs
        jobs.create('Enrich memories', 'backfill', 'memory_backfill')


def maintain():
    """Nightly tidy-up. Merges near-duplicates, retires stale low-value memories, expires working notes, links notes to the entities
    they mention and fills missing embeddings."""
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
                db.execute('INSERT OR IGNORE INTO memory_entities(memory_id,entity_id) SELECT ?,entity_id FROM memory_entities WHERE memory_id=?',
                           (keep['id'], other['id']))
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
    expired = len(db.rows("SELECT id FROM memories WHERE status='active' AND pinned=0 AND expires_at!='' AND expires_at<?", (db.now(),)))
    if expired:
        db.execute("UPDATE memories SET status='archived',updated_at=? WHERE status='active' AND pinned=0 AND expires_at!='' AND expires_at<?", (db.now(), db.now()))
    linked = 0
    try:
        entities.ensure_projects(force=True)
        have = {(r['memory_id'], r['entity_id']) for r in db.rows('SELECT memory_id,entity_id FROM memory_entities')}
        for r in rows:
            if r['id'] in archived:
                continue
            for eid in entities.find(r['content']):
                if (r['id'], eid) not in have:
                    entities.link_memory(r['id'], [eid])
                    linked += 1
    except Exception:
        pass
    embedded = backfill_embeddings()
    _touch()
    try:
        _queue_backfill()
    except Exception:
        pass
    summary = f'Merged {merged} duplicate memories, retired {stale} stale ones, embedded {embedded}.'
    if expired or linked:
        summary += f' Expired {expired} working notes, linked {linked} notes to entities.'
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
    mid = save(f'Daily summary {stamp}: {text}', 'daily_summary', 'daily digest', 0.7, importance=2, quiet=True, tier='episodic')
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
