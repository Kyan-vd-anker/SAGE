"""Feedback on answers: what was used to write each reply, and whether the user liked it.

agent.respond() leaves a trace for every assistant message (which memories, library passages and entities went into the prompt, which
tools ran, which model answered). A thumbs up/down (web UI, or "good answer" / "bad answer" in any chat, Telegram or by voice) is stored
against that message and nudges the memories involved: each memory carries a bounded feedback_score (-5..+5) that memory.relevant()
adds to its ranking with a small weight. Memories that keep appearing in good answers drift up, ones behind bad answers drift down.
Nothing is deleted or rewritten, and the effect is capped so one bad day cannot bury a fact.

evals.py can export the thumbs-down answers as candidate test cases.
"""
import json

from . import db


def _ids(value):
    try:
        return [int(x) for x in json.loads(value or '[]')]
    except (ValueError, TypeError):
        return []


def record_trace(message_id, user_text='', memory_ids=(), library_refs=(), entity_ids=(), tools=(), model='', source='chat'):
    if not message_id:
        return
    db.execute('INSERT OR REPLACE INTO answer_trace(message_id,user_text,memory_ids,library_refs,entity_ids,tools,model,source,created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?)',
               (int(message_id), str(user_text or '')[:1500], json.dumps(list(memory_ids)), json.dumps(list(library_refs)),
                json.dumps(list(entity_ids)), json.dumps(list(tools)), str(model or '')[:80], str(source or 'chat')[:20], db.now()))


def last_assistant_id():
    rows = db.rows("SELECT id FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")
    return rows[0]['id'] if rows else None


def rate(message_id, rating, reason=''):
    """Store a thumbs up (1) or down (-1) for an assistant message and adjust the memories behind it. -> a summary dict."""
    message_id = int(message_id)
    rating = 1 if int(rating) > 0 else -1
    message = db.rows("SELECT id FROM messages WHERE id=? AND role='assistant'", (message_id,))
    if not message:
        raise ValueError('That answer no longer exists')
    previous = db.rows('SELECT rating FROM feedback WHERE message_id=?', (message_id,))
    delta = rating - (previous[0]['rating'] if previous else 0)
    db.execute('INSERT OR REPLACE INTO feedback(id,message_id,rating,reason,created_at) VALUES('
               '(SELECT id FROM feedback WHERE message_id=?),?,?,?,?)', (message_id, message_id, rating, str(reason or '')[:500], db.now()))
    trace = db.rows('SELECT memory_ids FROM answer_trace WHERE message_id=?', (message_id,))
    memory_ids = _ids(trace[0]['memory_ids']) if trace else []
    if memory_ids and delta:
        marks = ','.join('?' for _ in memory_ids)
        db.execute(f'UPDATE memories SET feedback_score=MAX(-5,MIN(5,feedback_score+?)) WHERE id IN ({marks})', (delta, *memory_ids))
        try:
            from . import memory
            memory._touch()
        except Exception:
            pass
    return {'message_id': message_id, 'rating': rating, 'memories_adjusted': len(memory_ids) if delta else 0}


def stats():
    rows = db.rows('SELECT rating,COUNT(*) AS n FROM feedback GROUP BY rating')
    counts = {r['rating']: r['n'] for r in rows}
    return {'up': counts.get(1, 0), 'down': counts.get(-1, 0), 'traced_answers': db.rows('SELECT COUNT(*) AS n FROM answer_trace')[0]['n']}


def candidates(limit=50, rating=-1):
    """Rated answers with the question, the answer and what was in the prompt: raw material for new eval cases."""
    out = []
    for row in db.rows('SELECT f.message_id,f.rating,f.reason,f.created_at,m.content AS answer,t.user_text,t.memory_ids,t.library_refs,t.tools,t.model '
                       'FROM feedback f JOIN messages m ON m.id=f.message_id LEFT JOIN answer_trace t ON t.message_id=f.message_id '
                       'WHERE f.rating=? ORDER BY f.id DESC LIMIT ?', (int(rating), int(limit))):
        question = row['user_text'] or ''
        if not question:
            prev = db.rows("SELECT content FROM messages WHERE id<? AND role='user' ORDER BY id DESC LIMIT 1", (row['message_id'],))
            question = prev[0]['content'] if prev else ''
        out.append({'question': question, 'answer': row['answer'][:1500], 'rating': row['rating'], 'reason': row['reason'],
                    'memory_ids': _ids(row['memory_ids']), 'tools': json.loads(row['tools'] or '[]') if row['tools'] else [],
                    'model': row['model'], 'when': row['created_at']})
    return out
