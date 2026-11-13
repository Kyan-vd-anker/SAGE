"""Section-aware scientific summarisation.

The old summary read a paper as a stream of windows and wrote one compressed text. This module reads it the way a student
would: section by section, each with a question that fits the section (what problem, what method and conditions, which results
with numbers, what comparison, what limitation), keeps only notes that survive verification against their own page, collects
the technical terms the paper defines, and only then writes a synthesis in a fixed scientific structure.

Provenance is explicit in the output:
  * plain sentences restate the paper and carry a page citation that was checked;
  * sentences that connect or explain notes start with "Interpretation:", still cite the pages they rest on, and are held to
    the same checks;
  * nothing comes from outside knowledge (external research is not part of this pipeline).
"""
import re

from . import docqa, scidoc

MAP_TOKENS = 900
MAX_NOTES_PER_UNIT = 14
MAX_TERMS = 14
TAG_RANK = {'finding': 1, 'compare': 2, 'mechanism': 3, 'limit': 4, 'future': 5, 'method': 6, 'background': 7, 'definition': 8}
SOURCES = ('own', 'prior', 'review')
ROLE_SCORE = {'abstract': 9, 'conclusion': 9, 'results': 8, 'discussion': 8, 'topic': 7, 'methods': 6, 'introduction': 5, '': 4}

GUIDES = {
    'abstract': 'the objective, the approach and the main message of the work',
    '': 'the objective, the approach and the main message of the work',
    'introduction': 'why the subject matters, the problem or gap, definitions of key concepts, and what the work aims to do',
    'methods': 'materials, steps, conditions and settings, copying every number and unit',
    'results': 'each result with its numbers, units and conditions, and what it was compared with',
    'discussion': 'the explanations the text gives for its results, comparisons made, and limitations named',
    'conclusion': 'the conclusions, the challenges named and the future directions proposed',
    'topic': 'which approach, material or technology is described, how it works (the mechanism as the text explains it), what results are '
             'reported with their numbers and conditions, and how it compares with the alternatives the text names',
}

_NOTE = re.compile(r'^\s*[-*\u2022]?\s*\[(?:(D\d+)\s*)?(?:p\.?|part\s*)(\d+)\]\s*\[(\w+)(?:\s*\|\s*(\w+))?\]\s*(.+)$', re.I)
_TERM = re.compile(r'^\s*[-*\u2022]?\s*TERM\s*\[(?:(D\d+)\s*)?(?:p\.?|part\s*)(\d+)\]\s*(.{2,80}?)\s*::\s*(.{10,400})$', re.I)

_STRUCTURE = {
    'review article': (
        'This document is a review article: it reports and organises other researchers\' work, so describe it that way.',
        'Use exactly these headings, in this order, and skip a heading only when the notes support nothing for it (then write "Not stated in the document."):\n'
        '## Short overview\n## Scientific background\n## Main scientific concepts\n## Approaches and technologies covered\n## Key findings reported\n'
        '## Comparisons\n## Limitations and challenges\n## Conclusions and future directions\n## Important terminology'),
    'experimental research article': (
        'This document is an experimental research article.',
        'Use exactly these headings, in this order, and skip a heading only when the notes support nothing for it (then write "Not stated in the document."):\n'
        '## Short overview\n## Scientific background\n## Main scientific concepts\n## Methods and experimental conditions\n## Key findings\n'
        '## Comparisons\n## Limitations and challenges\n## Conclusions and future directions\n## Important terminology'),
}
_GENERIC = ('The document type could not be established, so make no claim about it.',
            'Use these headings, in this order, and leave out any the notes do not support:\n'
            '## Short overview\n## Scientific background\n## Main scientific concepts\n## Methods and approaches\n## Key findings\n'
            '## Comparisons\n## Limitations and challenges\n## Conclusions and future directions\n## Important terminology')

_RULES = ('HOW TO WRITE IT\n'
          '- Explain the subject like a scientist writing for a student: say what each concept is, how the mechanism works and how variables and properties relate, '
          'but only as far as the notes say it.\n'
          '- Put every number with its condition and unit. Compare approaches only where the notes compare or describe both.\n'
          '- A sentence that restates a note needs only that note\'s [D1 p.N] citation.\n'
          '- A sentence that connects notes or explains why a finding matters is your own reasoning: start it with "Interpretation:", keep it short, and cite the notes it rests on.\n'
          '- Never add a fact, mechanism, number or comparison that is not in the notes. Never use outside knowledge.\n'
          '- Under "Important terminology" give each term with its definition from the TERMS list. Do not repeat the document identification.')

LEGEND = ('*Reading guide: unmarked statements restate the paper and cite the page they came from. Statements starting with '
          '"Interpretation:" are Sage\'s reasoning from the cited pages. Nothing here comes from outside the paper.*')


def plan_units(doc, window_chars, cap):
    """Split the paper into reading units along its own sections. -> (units, skipped_units).
    A unit is {'headings','role','chunks','pages','size'}. Small neighbouring sections share a unit, big sections are split;
    when there are more units than `cap`, the least informative sections are the ones left out (and reported)."""
    by_index = {c.index: c for c in doc.body_chunks()}
    sections = [s for s in (doc.structure or {}).get('sections', []) if s['role'] != 'backmatter']
    if not sections:
        sections = [{'heading': 'Document', 'role': '', 'pages': [1, doc.n_pages], 'chunks': [c.index for c in doc.body_chunks()]}]
    pieces = []
    for section in sections:
        part, size = [], 0
        for index in section['chunks']:
            chunk = by_index.get(index)
            if chunk is None:
                continue
            cost = len(chunk.text) + 24
            if part and size + cost > window_chars:
                pieces.append((section, part))
                part, size = [], 0
            part.append(chunk)
            size += cost
        if part:
            pieces.append((section, part))
    units, current = [], None
    for section, chunks in pieces:
        cost = sum(len(c.text) + 24 for c in chunks)
        if current and current['size'] + cost <= window_chars and (section['role'] == current['role'] or current['size'] < 0.35 * window_chars):
            current['chunks'].extend(chunks)
            current['size'] += cost
            if section['heading'] not in current['headings']:
                current['headings'].append(section['heading'])
        else:
            if current:
                units.append(current)
            current = {'headings': [section['heading']], 'role': section['role'], 'chunks': list(chunks), 'size': cost}
    if current:
        units.append(current)
    for unit in units:
        unit['pages'] = (unit['chunks'][0].page, unit['chunks'][-1].page)
    skipped = []
    if len(units) > cap:
        order = sorted(range(len(units)), key=lambda i: (ROLE_SCORE.get(units[i]['role'], 4), i))
        drop = set(order[:len(units) - cap])
        skipped = [units[i] for i in sorted(drop)]
        units = [u for i, u in enumerate(units) if i not in drop]
    return units, skipped


def _unit_label(unit):
    heads = unit['headings']
    name = heads[0] if len(heads) == 1 else f'{heads[0]} \u2026 {heads[-1]}'
    first, last = unit['pages']
    return f'{name} (pages {first}\u2013{last})' if first != last else f'{name} (page {first})'


def _map_prompt(doc, unit, header, source_hint):
    passages = '\n\n'.join(f'{doc.cite(c.page)} {c.text}' for c in unit['chunks'])
    guide = GUIDES.get(unit['role'], GUIDES['topic'])
    return (f'{header}\n\nSECTION: {_unit_label(unit)}; role: {unit["role"] or "front matter"}\n\nPASSAGES\n{passages}\n\n'
            f'TASK: List the facts stated in these passages that matter for a scientific summary, at most {MAX_NOTES_PER_UNIT} lines. For this section focus on: {guide}. '
            f'One line per fact, in exactly this format: - [{doc.label} p.N] [tag|source] fact. '
            'tag is one of: finding (a result or claim), mechanism (how or why something works, as the text explains it), method (how something is made or done), '
            'compare (an explicit comparison between approaches, materials or conditions), limit (a limitation, challenge or drawback), future (proposed future work), '
            f'background (established concept or motivation). source is one of: own (the authors\' own experiment), prior (earlier research the text describes), review. {source_hint} '
            f'Copy numbers and units exactly. Do not add anything that is not in the passages. Then, if the passages define or explain technical terms, add up to 3 lines: '
            f'TERM [{doc.label} p.N] name :: definition, using the passage\'s own words.')


def _read_unit(doc, unit, ask, num_ctx, system_extra, header, source_hint):
    def build(scale):
        shown = dict(unit)
        if scale < 1.0:
            shown['chunks'] = unit['chunks'][:max(1, int(len(unit['chunks']) * scale))]
        return docqa._messages(system_extra, _map_prompt(doc, shown, header, source_hint))
    text = docqa._guarded(ask, build, num_ctx, MAP_TOKENS)
    notes, found_terms, dropped = [], [], 0
    low, high = unit['pages'][0] - 1, unit['pages'][1] + 1
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        term = _TERM.match(line)
        if term:
            page = int(term.group(2))
            name, definition = term.group(3).strip(' *'), term.group(4).strip()
            if low <= page <= high and not docqa.verify_answer(f'{name} {definition} {doc.cite(page)}', [doc]).flagged \
                    and name.lower() in doc.page_norm[page - 1] if 1 <= page <= doc.n_pages else False:
                found_terms.append({'page': page, 'name': name, 'definition': definition})
            else:
                dropped += 1
            continue
        note = _NOTE.match(line)
        if not note:
            continue
        page, tag, source, fact = int(note.group(2)), note.group(3).lower(), (note.group(4) or '').lower(), note.group(5).strip()
        if not low <= page <= high or docqa.verify_answer(f'{fact} {doc.cite(page)}', [doc]).flagged:
            dropped += 1
            continue
        if tag in ('own', 'prior'):
            source, tag = tag, 'finding'
        tag = tag if tag in TAG_RANK else 'finding'
        notes.append({'page': page, 'tag': tag, 'source': source if source in SOURCES else '', 'text': fact,
                      'number': bool(docqa._NUM_UNIT.search(fact))})
    return notes[:MAX_NOTES_PER_UNIT], found_terms[:3], dropped


def _note_line(doc, note):
    label = note['tag'] + (f"|{note['source']}" if note['source'] else '')
    return f"- {doc.cite(note['page'])} ({label}) {note['text']}"


def select_notes(doc, per_unit, budget):
    """Fit as many verified notes as the final prompt allows, taking the most informative first and spreading them over all
    sections instead of letting the first sections use the whole budget. -> prompt text"""
    ranked = []
    for unit, notes in per_unit:
        ordered = sorted(notes, key=lambda n: (0 if n['number'] else TAG_RANK.get(n['tag'], 9), n['page']))
        ranked.append((unit, ordered))
    chosen = {id(unit): [] for unit, _ in ranked}
    size = sum(len(_unit_label(u)) + 8 for u, notes in ranked if notes)
    rounds = max((len(n) for _, n in ranked), default=0)
    for r in range(rounds):
        for unit, notes in ranked:
            if r < len(notes):
                line = _note_line(doc, notes[r])
                if size + len(line) + 1 <= budget:
                    chosen[id(unit)].append(notes[r])
                    size += len(line) + 1
    blocks = []
    for unit, _ in ranked:
        kept = sorted(chosen[id(unit)], key=lambda n: n['page'])
        if kept:
            blocks.append(f'== {_unit_label(unit)} ==\n' + '\n'.join(_note_line(doc, n) for n in kept))
    return '\n\n'.join(blocks)


def _captions_block(doc, limit=8):
    caps = (doc.structure or {}).get('captions') or []
    if not caps:
        return ''
    lines = [f"- {c['label']} ({doc.unit}{c['page']}): {c['text']}" for c in caps[:limit]]
    more = f'\n- \u2026and {len(caps) - limit} more.' if len(caps) > limit else ''
    return '\n\n### Figures and tables in the paper (captions, quoted from the file)\n' + '\n'.join(lines) + more


def _warnings_block(doc):
    warnings = list(doc.warnings or [])
    if (doc.pdf_meta or {}).get('columns'):
        pages = doc.pdf_meta['columns']
        warnings.append(f'Two-column layout was detected on {len(pages)} page{"s" if len(pages) != 1 else ""} and read column by column.')
    if (doc.structure or {}).get('equations'):
        warnings.append('Equations are extracted as plain text and may lose their layout; check formulas against the PDF.')
    return ('\n\n*Extraction notes: ' + ' '.join(warnings) + '*') if warnings else ''


def summarize_one(doc, ask, emit, num_ctx, answer_tokens, map_calls):
    """Verified, section-aware summary of one document. -> docqa.Answer"""
    system_extra = docqa._type_note(doc)
    header = docqa._doc_header(doc)
    note, structure = _STRUCTURE.get(doc.meta.get('type'), _GENERIC)
    fixed = docqa.SYSTEM_PROMPT + header + structure + _RULES
    final_budget = docqa._budget_chars(num_ctx, answer_tokens, fixed)
    source_hint = 'In a review article nearly everything describes earlier work, so use prior unless the authors state their own experiment.' \
        if doc.meta.get('type') == 'review article' else ''
    prefix = docqa.identification_block(doc) + '\n\n' + LEGEND
    suffix_extra = _captions_block(doc) + _warnings_block(doc)
    body_total = sum(len(c.text) + 24 for c in doc.body_chunks())
    report = {'doc': doc.name, 'mode': 'single-pass' if body_total <= final_budget else 'map-reduce'}
    coverage_note = ''
    glossary = []
    if body_total <= final_budget:
        def evidence_for(scale):
            return 'PASSAGES\n' + docqa.render_passages(docqa.overview_hits(doc, int(final_budget * scale)), int(final_budget * scale))[0]
    else:
        probe = _map_prompt(doc, {'headings': ['Section'], 'role': '', 'chunks': [], 'pages': (1, 1)}, header, source_hint)
        window_budget = docqa._budget_chars(num_ctx, MAP_TOKENS, docqa.SYSTEM_PROMPT + system_extra + probe)
        units, skipped = plan_units(doc, window_budget, max(1, map_calls))
        report.update({'sections': len(units) + len(skipped), 'units': len(units)})
        per_unit, dropped = [], 0
        for number, unit in enumerate(units, 1):
            docqa._emit(emit, f'Reading {doc.label} {_unit_label(unit)} ({number}/{len(units)})\u2026')
            notes, terms_found, bad = _read_unit(doc, unit, ask, num_ctx, system_extra, header, source_hint)
            per_unit.append((unit, notes))
            glossary.extend(terms_found)
            dropped += bad
        report['notes'] = sum(len(n) for _, n in per_unit)
        report['notes_dropped_unverified'] = dropped
        if skipped:
            pages = sorted({c.page for u in skipped for c in u['chunks']})
            names = ', '.join(u['headings'][0] for u in skipped[:5])
            coverage_note = (f'\n\n*Coverage: {len(units)} of {len(units) + len(skipped)} reading units were analysed. Not analysed: {names}'
                             f'{" and others" if len(skipped) > 5 else ""} (pages {docqa._page_ranges(pages)}).*')
            report['pages_not_analysed'] = pages
        if not report['notes']:
            return docqa.Answer(f'{prefix}\n\n{docqa._UNVERIFIED_BANNER}\n\nI could not extract statements I could check from the reading passes, so this is taken directly '
                                f'from the document\'s own opening and concluding passages:\n\n{docqa._extractive_summary(doc)}{suffix_extra}{coverage_note}',
                                'unverified', report)
        seen_terms, unique_terms = set(), []
        for item in glossary:
            if item['name'].lower() not in seen_terms:
                seen_terms.add(item['name'].lower())
                unique_terms.append(item)
        glossary = unique_terms[:MAX_TERMS]
        report['terms'] = len(glossary)
        terms_text = '\n'.join(f"TERM {doc.cite(t['page'])} {t['name']} :: {t['definition']}" for t in glossary)

        def evidence_for(scale):
            budget = int(final_budget * scale) - len(terms_text) - 40
            block = select_notes(doc, per_unit, max(600, budget))
            return 'VERIFIED NOTES (each was checked against its page)\n' + block + ('\n\nTERMS (verified definitions)\n' + terms_text if terms_text else '')
    feedback = ''
    for attempt in (1, 2):
        def build(scale, feedback=feedback):
            user = (f'{header}\n\n{evidence_for(scale)}\n\nTASK: Write a scientific synthesis of the document. {structure}\n\n{_RULES}\n'
                    f'Cite every sentence as [{doc.label} p.N] using the pages shown.{feedback}')
            return docqa._messages(system_extra + ('\n' + note if note else ''), user)
        docqa._emit(emit, f'Writing the summary of {doc.label}\u2026' if attempt == 1 else 'Re-checking the summary against the source\u2026')
        text = docqa._guarded(ask, build, num_ctx, answer_tokens)
        docqa._emit(emit, 'Verifying the summary against the document\u2026')
        outcome = docqa._finalize(text, [doc], attempt, prefix=prefix, suffix=suffix_extra + coverage_note)
        if outcome.status != 'retry':
            outcome.report.update(report)
            if outcome.status == 'unverified':
                outcome.text = docqa._unverified_summary(doc, prefix, outcome.report, suffix_extra + coverage_note)
            elif outcome.status == 'error':
                outcome.text = (f'{prefix}\n\n{docqa._UNVERIFIED_BANNER}\n\nThe model returned nothing usable, so this is taken directly from the document:\n\n'
                                f'{docqa._extractive_summary(doc)}{suffix_extra}{coverage_note}')
                outcome.status = 'unverified'
            return outcome
        feedback = outcome.report['feedback']
    return docqa.Answer(docqa._unverified_summary(doc, prefix, report, suffix_extra + coverage_note), 'unverified', report)
