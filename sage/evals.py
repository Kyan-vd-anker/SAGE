"""Evals: a fixed set of questions with known answers, so every change to Sage's memory can be measured instead of guessed at.

    python -m sage.evals run                 fixture suite (evals/cases.json) in a throw-away database: deterministic, no model, a few seconds
    python -m sage.evals run --suite live    your own questions (data/evals/live.json) against your real memory and library
    python -m sage.evals run --answers       also ask the local model the "answer" cases (slow; needs Ollama)
    python -m sage.evals tune [--apply]      search the retrieval weights (entity bonus, importance, recency, feedback) for a better MRR
    python -m sage.evals export              write the thumbs-down answers to data/evals/from_feedback.json, ready to turn into cases
    python -m sage.evals history             the last runs

A case file looks like {"suite": "fixture", "fixture": {...}, "cases": [...]}. Case types:
    retrieval   {"query", "expect_any": [text], "k": 3, "absent": [text]}   memory.relevant() must return a match in the top k
    no_answer   {"query"}                                                  nothing may be retrieved (precision)
    recall      {"query", "expect_any", "tier", "days", "include_history"}  the deep-recall tool must surface the text
    library     {"query", "expect_file", "k"} / "library_none"             passages from the user's own files (none for secrets)
    core        {"expect_any", "absent"}                                   the always-on profile block
    answer      {"question", "expect_regex", "absent_regex"}               a real model answer (only with --answers)

Every run is stored in the eval_runs table; the report lists regressions against the previous run of the same suite.
"""
import argparse
import contextlib
import itertools
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, entities, memory, userprofile

CASES_FILE = Path(config.ROOT) / 'evals' / 'cases.json'
GRID = {'entity': (0.15, 0.30, 0.45), 'importance': (0.02, 0.04, 0.08), 'recency': (0.0, 0.08, 0.16), 'feedback': (0.0, 0.03, 0.06)}
_PATCHED = ('DATA', 'DB_PATH', 'VAULT', 'WORKSPACE', 'LIBRARY_ROOTS', 'REPO_ROOTS', 'CHRONICLE_ENABLED', 'LIBRARY_ENABLED')


def live_file():
    return Path(config.DATA) / 'evals' / 'live.json'


# ------------------------------------------------------------------ a throw-away world

@contextlib.contextmanager
def sandbox(semantic=False):
    """A temporary database, vault and library, so a fixture run never touches the real memory."""
    saved = {name: getattr(config, name) for name in _PATCHED}
    weights = memory._W['override']
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'library').mkdir()
        config.DATA, config.DB_PATH, config.VAULT, config.WORKSPACE = root, root / 'sage.db', root / 'vault', root / 'workspace'
        config.LIBRARY_ROOTS, config.REPO_ROOTS, config.CHRONICLE_ENABLED, config.LIBRARY_ENABLED = str(root / 'library'), '', True, True
        memory._W['override'] = None
        memory._W['db'] = None
        if not semantic:
            memory._semantic.update(ok=False, at=time.time() + 86400 * 365)      # never call the embedding model
        memory._touch()
        try:
            db.init()
            yield root
        finally:
            for name, value in saved.items():
                setattr(config, name, value)
            memory._W['override'] = weights
            memory._W['db'] = None
            memory._semantic.update(ok=None, at=0.0)
            memory._touch()


def load_fixture(fixture, root):
    """Create the memories, entities, files and events a suite describes."""
    from . import chronicle, library
    for item in fixture.get('entities', []):
        eid = entities.ensure(item['name'], item.get('kind', 'thing'), item.get('description', ''), item.get('aliases', ()))
        for relation in item.get('relations', []):
            other = entities.ensure(relation['to'], relation.get('kind', 'thing'))
            entities.relate(eid, other, relation.get('rel', 'related'))
    for item in fixture.get('memories', []):
        mid = memory.save(item['text'], item.get('kind', 'fact'), 'eval', 0.9, importance=item.get('importance'), tier=item.get('tier'),
                          quiet=True, entity_names=item.get('entities'), distinct=item.get('distinct', False))
        if item.get('pinned'):
            memory.update_memory(mid, pinned=True)
        if item.get('age_days'):
            stamp = (datetime.now(timezone.utc) - timedelta(days=int(item['age_days']))).isoformat(timespec='seconds')
            db.execute('UPDATE memories SET updated_at=?,created_at=? WHERE id=?', (stamp, stamp, mid))
    for relative, text in (fixture.get('files') or {}).items():
        path = Path(config.LIBRARY_ROOTS) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    if fixture.get('files'):
        library.scan()
    for event in fixture.get('events', []):
        if event['source'] == 'workshop':
            db.execute('INSERT INTO workshop_entries(note,created_at) VALUES(?,?)', (event['note'], db.now()))
        elif event['source'] == 'decision':
            db.execute('INSERT INTO decision_log(title,decision,rationale,created_at) VALUES(?,?,?,?)',
                       (event['title'], event['decision'], event.get('why', ''), db.now()))
        elif event['source'] == 'failure':
            db.execute('INSERT INTO failure_lessons(title,symptom,cause,fix,lesson,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (event['title'], event['symptom'], event.get('cause', ''), event.get('fix', ''), event.get('lesson', ''), db.now(), db.now()))
    chronicle.drain()


# ------------------------------------------------------------------ running cases

def _has(text, needles):
    low = str(text).lower()
    return any(str(n).lower() in low for n in needles)


def _retrieve(query, k, semantic=False):
    _, core_ids = userprofile.core()
    return memory.relevant(query, limit=k, track=False, semantic=semantic, entity_ids=entities.find(query), exclude_ids=core_ids)


def run_case(case, answers=False, semantic=False):
    """-> {'id', 'type', 'pass', 'rank', 'note', 'skipped'}"""
    kind, k = case.get('type', 'retrieval'), int(case.get('k', 3))
    out = {'id': case['id'], 'type': kind, 'tags': case.get('tags', []), 'pass': False, 'rank': None, 'note': '', 'skipped': False}
    try:
        if kind in ('retrieval', 'no_answer'):
            hits = _retrieve(case['query'], k, semantic)
            if kind == 'no_answer':
                out['pass'] = not hits
                out['note'] = '' if not hits else 'retrieved: ' + hits[0]['content'][:70]
            else:
                for position, hit in enumerate(hits, 1):
                    if _has(hit['content'], case['expect_any']):
                        out['rank'] = position
                        break
                bad = [h for h in hits if case.get('absent') and _has(h['content'], case['absent'])]
                out['pass'] = out['rank'] is not None and not bad
                out['note'] = 'unexpected: ' + bad[0]['content'][:70] if bad else ('' if out['rank'] else 'no expected memory in top ' + str(k))
        elif kind == 'recall':
            result = memory.recall(case['query'], tier=case.get('tier'), days=case.get('days'), include_history=bool(case.get('include_history')))
            blob = json.dumps(result, ensure_ascii=False)
            out['pass'] = _has(blob, case['expect_any']) and not (case.get('absent') and _has(blob, case['absent']))
            out['note'] = '' if out['pass'] else 'expected text not in the recall result'
        elif kind in ('library', 'library_none'):
            from . import library
            hits = library.search(case['query'], limit=k, semantic=semantic)
            if kind == 'library_none':
                out['pass'] = not hits or not _has(json.dumps(hits), case.get('absent', [case['query']]))
                out['note'] = '' if out['pass'] else 'secret text was indexed'
            else:
                for position, hit in enumerate(hits, 1):
                    if str(case['expect_file']).lower() in hit['path'].lower().replace('\\', '/'):
                        out['rank'] = position
                        break
                out['pass'] = out['rank'] is not None
                out['note'] = '' if out['pass'] else 'file not in top ' + str(k)
        elif kind == 'core':
            text, _ = userprofile.core()
            out['pass'] = _has(text, case['expect_any']) and not (case.get('absent') and _has(text, case['absent']))
            out['note'] = '' if out['pass'] else 'core block missing/extra text'
        elif kind == 'answer':
            if not answers:
                out.update(skipped=True, note='needs --answers (real model)')
                return out
            from . import agent
            before = db.rows('SELECT COALESCE(MAX(id),0) AS i FROM messages')[0]['i']
            try:
                reply = agent.respond(case['question'], allow_memory_capture=False)
            finally:
                db.execute('DELETE FROM messages WHERE id>?', (before,))
                db.execute('DELETE FROM answer_trace WHERE message_id>?', (before,))
            ok = all(re.search(p, reply) for p in case.get('expect_regex', []))
            bad = [p for p in case.get('absent_regex', []) if re.search(p, reply)]
            out['pass'] = ok and not bad
            out['note'] = '' if out['pass'] else 'answer: ' + reply[:90].replace('\n', ' ')
        else:
            out.update(skipped=True, note=f'unknown case type {kind}')
    except Exception as exc:
        out['note'] = f'error: {exc}'[:160]
    return out


def summarise(results):
    scored = [r for r in results if not r['skipped']]
    ranked = [r for r in scored if r['type'] in ('retrieval', 'library')]
    mrr = sum((1 / r['rank']) if r['rank'] else 0.0 for r in ranked) / len(ranked) if ranked else None
    by_tag = {}
    for r in scored:
        for tag in r['tags'] or ['untagged']:
            item = by_tag.setdefault(tag, [0, 0])
            item[0] += 1 if r['pass'] else 0
            item[1] += 1
    return {'total': len(scored), 'passed': sum(1 for r in scored if r['pass']), 'skipped': len(results) - len(scored),
            'mrr': round(mrr, 4) if mrr is not None else None, 'by_tag': {t: f'{a}/{b}' for t, (a, b) in sorted(by_tag.items())}}


def load_suite(name):
    path = CASES_FILE if name == 'fixture' else live_file()
    if not path.exists():
        raise SystemExit(f'No case file at {path}' + ('' if name == 'fixture' else ' (copy evals/live.example.json there and edit it)'))
    return json.loads(path.read_text(encoding='utf-8'))


def run_suite(name='fixture', answers=False, tag=None, semantic=False, store=True):
    """Run one suite. -> {'suite', 'summary', 'results', 'regressions', 'fixed'}"""
    data = load_suite(name)
    cases = [c for c in data['cases'] if not tag or tag in c.get('tags', [])]
    if name == 'fixture':
        with sandbox(semantic) as root:
            load_fixture(data.get('fixture', {}), root)
            results = [run_case(c, answers, semantic) for c in cases]
        return _finish(name, data, results, store=store)   # history lives in the real database, outside the throw-away one
    results = [run_case(c, answers, semantic) for c in cases]
    return _finish(name, data, results, store=store)


def _finish(name, data, results, store):
    summary = summarise(results)
    previous = None
    if store:
        # fixture runs happen in a throw-away database, so history is kept in the real one
        rows = db.rows('SELECT results_json FROM eval_runs WHERE suite=? ORDER BY id DESC LIMIT 1', (name,))
        previous = {r['id']: r['pass'] for r in json.loads(rows[0]['results_json'])} if rows else None
        db.execute('INSERT INTO eval_runs(suite,summary_json,results_json,created_at) VALUES(?,?,?,?)',
                   (name, json.dumps(summary), json.dumps([{k: r[k] for k in ('id', 'pass', 'rank')} for r in results]), db.now()))
    regressions = [r['id'] for r in results if previous and previous.get(r['id']) and not r['pass'] and not r['skipped']]
    fixed = [r['id'] for r in results if previous and previous.get(r['id']) is False and r['pass']]
    return {'suite': name, 'summary': summary, 'results': results, 'regressions': regressions, 'fixed': fixed}


# ------------------------------------------------------------------ tuning

def _mrr(cases, weights, semantic=False):
    memory._W['override'] = weights
    total, count = 0.0, 0
    for case in cases:
        k = int(case.get('k', 3))
        hits = _retrieve(case['query'], k, semantic)
        count += 1
        if case['type'] == 'no_answer':
            total += 0.0 if hits else 1.0
            continue
        rank = next((p for p, h in enumerate(hits, 1) if _has(h['content'], case['expect_any'])), None)
        bad = any(case.get('absent') and _has(h['content'], case['absent']) for h in hits)
        total += (1.0 / rank) if rank and not bad else 0.0
    return total / count if count else 0.0


def tune(suite='fixture', apply=False, semantic=False):
    """Grid-search the ranking weights. A change is only proposed when it beats the current weights by 0.02 MRR (guards against overfitting)."""
    data = load_suite(suite)
    cases = [c for c in data['cases'] if c.get('type') in ('retrieval', 'no_answer')]

    def search():
        current = dict(memory.weights())
        base = _mrr(cases, current, semantic)
        best, best_score = current, base
        for values in itertools.product(*GRID.values()):
            candidate = {**current, **dict(zip(GRID, values))}
            score = _mrr(cases, candidate, semantic)
            distance = sum(abs(candidate[k] - current[k]) for k in GRID)
            if score > best_score + 1e-9 or (abs(score - best_score) < 1e-9 and best is not current and distance < sum(abs(best[k] - current[k]) for k in GRID)):
                best, best_score = candidate, score
        return current, base, best, best_score

    if suite == 'fixture':
        with sandbox(semantic) as root:
            load_fixture(data.get('fixture', {}), root)
            current, base, best, best_score = search()
    else:
        current, base, best, best_score = search()
    memory._W['override'] = None
    gain = best_score - base
    proposal = {k: best[k] for k in GRID} if gain >= 0.02 else None
    if proposal and apply and suite != 'fixture':
        memory.set_weights({**current, **proposal})
    return {'cases': len(cases), 'current_mrr': round(base, 4), 'best_mrr': round(best_score, 4), 'gain': round(gain, 4), 'proposal': proposal,
            'applied': bool(proposal and apply and suite != 'fixture')}


# ------------------------------------------------------------------ feedback export

def export_feedback(limit=100):
    from . import feedback
    items = feedback.candidates(limit=limit, rating=-1)
    path = Path(config.DATA) / 'evals' / 'from_feedback.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    cases = [{'id': f'feedback-{i + 1}', 'type': 'retrieval', 'query': it['question'], 'expect_any': ['FILL IN: text the right memory contains'],
              'k': 3, 'tags': ['feedback'], 'note': f"thumbs down: {it['reason'] or 'no reason given'}; answer began: {it['answer'][:80]}"}
             for i, it in enumerate(items) if it['question']]
    path.write_text(json.dumps({'suite': 'live', 'cases': cases}, indent=2, ensure_ascii=False), encoding='utf-8')
    return str(path), len(cases)


# ------------------------------------------------------------------ command line

def _print_run(run):
    s = run['summary']
    print(f"\nSuite '{run['suite']}': {s['passed']}/{s['total']} passed" + (f", MRR {s['mrr']}" if s['mrr'] is not None else '') + (f", {s['skipped']} skipped" if s['skipped'] else ''))
    for tag, value in s['by_tag'].items():
        print(f'  {tag:<14}{value}')
    for r in run['results']:
        if not r['pass'] and not r['skipped']:
            print(f"  FAIL {r['id']}: {r['note']}")
    if run['regressions']:
        print('  REGRESSIONS since the last run: ' + ', '.join(run['regressions']))
    if run['fixed']:
        print('  Fixed since the last run: ' + ', '.join(run['fixed']))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m sage.evals', description="Measure Sage's memory and retrieval.")
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--suite', choices=('fixture', 'live', 'all'), default='fixture')
    run.add_argument('--answers', action='store_true', help='also ask the local model the answer cases')
    run.add_argument('--semantic', action='store_true', help='use the Ollama embedding model (needs Ollama running)')
    run.add_argument('--tag')
    tuning = sub.add_parser('tune')
    tuning.add_argument('--suite', choices=('fixture', 'live'), default='fixture')
    tuning.add_argument('--apply', action='store_true', help='store the better weights (live suite only)')
    sub.add_parser('export')
    sub.add_parser('history')
    args = parser.parse_args(argv)
    db.init()
    if args.command == 'run':
        failed = False
        for name in (('fixture', 'live') if args.suite == 'all' else (args.suite,)):
            run_result = run_suite(name, answers=args.answers, tag=args.tag, semantic=args.semantic)
            _print_run(run_result)
            failed = failed or run_result['summary']['passed'] < run_result['summary']['total']
        return 1 if failed else 0
    if args.command == 'tune':
        print(json.dumps(tune(args.suite, apply=args.apply), indent=2))
    elif args.command == 'export':
        path, count = export_feedback()
        print(f'Wrote {count} candidate cases to {path}. Fill in "expect_any" and move them into live.json.')
    elif args.command == 'history':
        for row in db.rows('SELECT id,suite,summary_json,created_at FROM eval_runs ORDER BY id DESC LIMIT 15'):
            s = json.loads(row['summary_json'])
            print(f"{row['created_at'][:16]}  {row['suite']:<8} {s['passed']}/{s['total']}  MRR {s['mrr']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
