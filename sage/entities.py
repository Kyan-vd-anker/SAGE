"""Sage's knowledge graph: the people, projects, devices and things Kyan talks about.

An entity has a name, a kind, a one-line description and any number of aliases ("the ROV", "rov project").
Memories are linked to the entities they mention, and entities are linked to each other ("ROV uses Arduino Nano").

Finding the entities in a message costs no model call: one compiled regex over every known alias, rebuilt only when the
alias table changes. That keeps the per-turn latency flat however large the graph grows.

    entities.find("how is the ROV going?")        -> [3]
    entities.cards_for_ids([3])                   -> [{'name': 'ROV', 'kind': 'project', 'facts': [...], 'related': [...]}]
"""
import re
import threading

from . import config, db

KINDS = ('person', 'project', 'device', 'place', 'org', 'concept', 'thing')
MIN_ALIAS = 3
# Words that must never become an alias: they would match almost every message.
BLOCKED = frozenset({
    'the', 'and', 'you', 'sage', 'user', 'this', 'that', 'new', 'old', 'test', 'tests', 'project', 'projects', 'plan', 'task', 'tasks',
    'file', 'files', 'code', 'app', 'web', 'pc', 'ai', 'thing', 'things', 'stuff', 'today', 'tomorrow', 'home', 'work', 'one', 'two',
    'ik', 'jij', 'het', 'een', 'dat', 'dit',
})
_SECRETISH = re.compile(r'(?i)\b(password|passcode|token|secret|api[ _-]?key|wachtwoord)\b')

_lock = threading.RLock()
_index = {'key': None, 'regex': None, 'map': {}}
_synced = {'key': None}


def norm(name):
    """The form used to compare names and aliases."""
    text = re.sub(r'\s+', ' ', str(name or '').strip().lower())
    return text.strip(' .,:;!?"\'()[]{}')


# ------------------------------------------------------------------ alias index

def _fingerprint():
    row = db.rows("SELECT (SELECT COUNT(*) FROM entity_aliases) AS a, (SELECT COUNT(*) FROM entities) AS c, "
                  "(SELECT COALESCE(MAX(updated_at),'') FROM entities) AS u")[0]
    return (str(config.DB_PATH), row['a'], row['c'], row['u'])


def _get_index():
    key = _fingerprint()
    with _lock:
        if _index['key'] == key:
            return _index
    mapping = {}
    for row in db.rows('SELECT alias,entity_id FROM entity_aliases'):
        alias = row['alias']
        if len(alias) >= MIN_ALIAS and alias not in BLOCKED:
            mapping[alias] = row['entity_id']
    regex = None
    if mapping:
        ordered = sorted(mapping, key=len, reverse=True)
        regex = re.compile(r'(?<![\w-])(?:' + '|'.join(re.escape(a) for a in ordered) + r')(?![\w-])', re.IGNORECASE | re.UNICODE)
    with _lock:
        _index.update(key=key, regex=regex, map=mapping)
        return _index


def ensure_projects(force=False):
    """Mirror the projects table into entities (kind 'project') so every project can be recognised by name."""
    row = db.rows('SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS i FROM projects')[0]
    key = (str(config.DB_PATH), row['n'], row['i'])
    if not force and _synced['key'] == key:
        return
    _synced['key'] = key
    for project in db.rows('SELECT id,name FROM projects'):
        name = ' '.join(str(project['name'] or '').split())
        if not name:
            continue
        aliases = []
        words = name.split()
        trimmed = list(words)
        while len(trimmed) > 1 and trimmed[-1].lower() in ('project', 'plan', 'initiative'):
            trimmed.pop()
        if trimmed != words:
            aliases.append(' '.join(trimmed))
        if len(words) > 1 and words[0].isupper() and len(words[0]) >= 3:
            aliases.append(words[0])                      # an acronym such as "ROV Underwater Vehicle"
        ensure(name, 'project', project_id=project['id'], aliases=aliases)


def find(text, limit=6):
    """Ids of the known entities mentioned in `text`, in order of first mention."""
    if not text:
        return []
    try:
        ensure_projects()
        index = _get_index()
    except Exception:
        return []
    if not index['regex']:
        return []
    found = []
    for match in index['regex'].finditer(str(text)[:4000]):
        eid = index['map'].get(norm(match.group(0)))
        if eid and eid not in found:
            found.append(eid)
            if len(found) >= limit:
                break
    return found


# ------------------------------------------------------------------ create and look up

def resolve(name):
    rows = db.rows('SELECT entity_id FROM entity_aliases WHERE alias=?', (norm(name),))
    return rows[0]['entity_id'] if rows else None


def get(entity_id):
    rows = db.rows('SELECT * FROM entities WHERE id=?', (int(entity_id),))
    return rows[0] if rows else None


def add_alias(entity_id, alias):
    text = norm(alias)
    if len(text) < 2 or len(text) > 60 or text in BLOCKED or _SECRETISH.search(text):
        return False
    db.execute('INSERT OR IGNORE INTO entity_aliases(alias,entity_id) VALUES(?,?)', (text, int(entity_id)))
    db.execute('UPDATE entities SET updated_at=? WHERE id=?', (db.now(), int(entity_id)))
    return True


def ensure(name, kind='thing', description='', aliases=(), project_id=None):
    """The id of the entity called `name`, creating it when it is new. Known aliases resolve to the existing entity."""
    clean = ' '.join(str(name or '').split())
    key = norm(clean)
    if len(key) < 2 or len(key) > 80 or key in BLOCKED or _SECRETISH.search(key):
        return None
    kind = kind if kind in KINDS else 'thing'
    description = ' '.join(str(description or '').split())[:300]
    with _lock:
        eid = resolve(key)
        stamp = db.now()
        if eid is None:
            try:
                eid = db.execute('INSERT INTO entities(name,kind,description,project_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                                 (clean[:80], kind, description, project_id, stamp, stamp))
            except Exception:
                eid = resolve(key)
                if eid is None:
                    return None
            add_alias(eid, key)
        else:
            current = get(eid)
            changes, args = [], []
            if description and (not current['description'] or len(description) > len(current['description'])):
                changes.append('description=?')
                args.append(description)
            if kind != 'thing' and current['kind'] == 'thing':
                changes.append('kind=?')
                args.append(kind)
            if project_id and not current['project_id']:
                changes.append('project_id=?')
                args.append(int(project_id))
            if changes:
                db.execute(f"UPDATE entities SET {','.join(changes)},updated_at=? WHERE id=?", (*args, stamp, eid))
        for alias in list(aliases or [])[:6]:
            add_alias(eid, alias)
        return eid


def merge(source_id, target_id):
    """Fold one entity into another (aliases, memory links and relations move over)."""
    source_id, target_id = int(source_id), int(target_id)
    if source_id == target_id or not get(source_id) or not get(target_id):
        return False
    with _lock:
        db.execute('UPDATE OR IGNORE entity_aliases SET entity_id=? WHERE entity_id=?', (target_id, source_id))
        db.execute('DELETE FROM entity_aliases WHERE entity_id=?', (source_id,))
        db.execute('UPDATE OR IGNORE memory_entities SET entity_id=? WHERE entity_id=?', (target_id, source_id))
        db.execute('DELETE FROM memory_entities WHERE entity_id=?', (source_id,))
        db.execute('UPDATE OR IGNORE entity_links SET src=? WHERE src=?', (target_id, source_id))
        db.execute('UPDATE OR IGNORE entity_links SET dst=? WHERE dst=?', (target_id, source_id))
        db.execute('DELETE FROM entity_links WHERE src=? OR dst=? OR src=dst', (source_id, source_id))
        db.execute('DELETE FROM entities WHERE id=?', (source_id,))
        db.execute('UPDATE entities SET updated_at=? WHERE id=?', (db.now(), target_id))
    return True


# ------------------------------------------------------------------ links

def link_memory(memory_id, entity_ids):
    for eid in entity_ids or []:
        db.execute('INSERT OR IGNORE INTO memory_entities(memory_id,entity_id) VALUES(?,?)', (int(memory_id), int(eid)))


def relate(a_id, b_id, rel='related'):
    if not a_id or not b_id or int(a_id) == int(b_id):
        return False
    label = re.sub(r'[^a-z0-9 _-]', '', str(rel or 'related').lower()).strip()[:30] or 'related'
    db.execute('INSERT INTO entity_links(src,dst,rel,weight,updated_at) VALUES(?,?,?,1,?) '
               'ON CONFLICT(src,dst,rel) DO UPDATE SET weight=MIN(weight+0.5,5), updated_at=excluded.updated_at',
               (int(a_id), int(b_id), label, db.now()))
    return True


def neighbors(entity_id, limit=3):
    rows = db.rows('SELECT CASE WHEN src=? THEN dst ELSE src END AS other, rel, weight FROM entity_links '
                   'WHERE src=? OR dst=? ORDER BY weight DESC, updated_at DESC LIMIT ?',
                   (int(entity_id), int(entity_id), int(entity_id), int(limit)))
    found = []
    for row in rows:
        other = get(row['other'])
        if other:
            found.append({'id': other['id'], 'name': other['name'], 'rel': row['rel']})
    return found


def project_ids(entity_ids):
    """Project ids behind the project entities in `entity_ids`, in order."""
    found = []
    for eid in entity_ids or []:
        entity = get(eid)
        if entity and entity['project_id'] and entity['project_id'] not in found:
            found.append(entity['project_id'])
    return found


# ------------------------------------------------------------------ what the model sees

def card(entity_id, max_facts=3, exclude=()):
    """A compact description of one entity, or None when there is nothing useful to say about it."""
    entity = get(entity_id)
    if not entity:
        return None
    skip = set(exclude or ())
    facts = db.rows("SELECT m.id,m.content FROM memories m JOIN memory_entities me ON me.memory_id=m.id "
                    "WHERE me.entity_id=? AND m.status='active' AND m.tier!='episodic' "
                    "ORDER BY m.pinned DESC, m.importance DESC, m.id DESC LIMIT ?", (int(entity_id), int(max_facts) + len(skip)))
    facts = [f['content'][:200] for f in facts if f['id'] not in skip][:max_facts]
    related = [f"{n['rel']} {n['name']}" for n in neighbors(entity_id, 3)]
    if not (facts or related or entity['description']):
        return None
    out = {'name': entity['name'], 'kind': entity['kind']}
    if entity['description']:
        out['about'] = entity['description'][:200]
    if facts:
        out['facts'] = facts
    if related:
        out['related'] = related
    return out


def cards_for_ids(entity_ids, limit=2, exclude=(), max_facts=3):
    cards = []
    for eid in entity_ids or []:
        item = card(eid, max_facts=max_facts, exclude=exclude)
        if item:
            cards.append(item)
        if len(cards) >= limit:
            break
    return cards


def top_names(limit=30):
    """The best-connected entities, shown to the extraction model so it reuses names instead of inventing duplicates."""
    return [{'name': r['name'], 'kind': r['kind']} for r in db.rows(
        'SELECT e.name,e.kind,COUNT(me.memory_id) AS c FROM entities e LEFT JOIN memory_entities me ON me.entity_id=e.id '
        'GROUP BY e.id ORDER BY c DESC, e.updated_at DESC LIMIT ?', (int(limit),))]


def listing(limit=25):
    return db.rows('SELECT e.id,e.name,e.kind,e.description,COUNT(me.memory_id) AS memories FROM entities e '
                   'LEFT JOIN memory_entities me ON me.entity_id=e.id GROUP BY e.id ORDER BY memories DESC, e.name LIMIT ?', (int(limit),))
