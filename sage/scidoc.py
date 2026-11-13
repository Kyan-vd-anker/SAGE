"""Scientific structure analysis for Sage's document pipeline.

Pure functions over page text, so they can be tested without a PDF or a model:

  * front matter: title (largest non-journal text on page 1), authors, journal and DOI - the journal banner is no longer
    mistaken for the title
  * running headers and page numbers: repeated edge lines are removed from pages 2+ and remembered as journal evidence
  * figure/table/scheme captions, table rows, numbered equations
  * the section outline with a role per section (abstract, introduction, methods, results, ..., or 'topic' for the subject
    sections of a review)
  * entities (acronyms and formulas such as PDMS, LCN, TiO2) used to catch statements that confuse two materials
"""
import re
import statistics
from collections import Counter

DOI = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)', re.I)
ROLES = (('abstract', r'abstract'), ('introduction', r'introduction|background|overview|motivation'),
         ('methods', r'method|experimental|materials|procedure|fabrication|synthesis|preparation|characteri[sz]ation'),
         ('results', r'result|finding'), ('discussion', r'discussion'),
         ('conclusion', r'conclusion|outlook|perspective|future|concluding|challenges|opportunit|summary'),
         ('backmatter', r'acknowledg|funding|conflict|author contribution|supplementary|supporting information|data availability|references?|bibliography'))

_NOISE = re.compile(r'(?i)(\bdoi\b|https?://|www\.|©|copyright|\breceived\b|\baccepted\b|\bpublished\b|available online|\bissn\b|\bvol\.|'
                    r'e-?mail|@|corresponding|\bdepartment\b|\buniversity\b|\binstitute\b|open access|\blicen[sc]e|^\s*\d+\s*$|'
                    r'^(?:review article|research article|perspective|communication|letter|full paper|article|review|mini review|'
                    r'original article|short communication|invited review|feature article)\s*$)')
_JOURNAL = re.compile(r"(?i)^(?:the )?(?:journal of|annals of|proceedings of|transactions on|advanced|nature|science|chemical|angewandte|"
                      r"macromolecules|acta |acs |rsc |ieee|physical review|applied physics|small\b|adv\.)|"
                      r"\b(?:reviews?|letters|journal|materials|chemistry|physics|polymers?|engineering|communications|advances|transactions)\s*$")
_CAPTION = re.compile(r'^\s*(Figure|Fig\.|Table|Scheme|Chart)\s*(S?\d+[A-Za-z]?)\s*[.:|\-\u2013]\s*(\S.{8,})', re.I)
_ENTITY = re.compile(r'\b(?:[A-Z]{2,}[a-z0-9]*|[A-Z][a-z]?[0-9]+[A-Za-z0-9]*|(?:[A-Z][a-z]?){2,}[0-9]*)\b')
_SMALL_WORDS = {'of', 'the', 'and', 'for', 'with', 'in', 'on', 'to', 'a', 'an', 'by', 'from', 'via', 'at', 'as', 'or'}


def role(section):
    low = (section or '').lower()
    for name, pattern in ROLES:
        if re.search(pattern, low):
            return name
    return ''


def find_doi(text):
    match = DOI.search(text or '')
    return match.group(1).rstrip('.,;)]}') if match else None


def journal_like(line):
    line = re.sub(r'\s+', ' ', line.strip())
    if not 4 <= len(line) <= 60 or len(line.split()) > 7 or line.endswith('.') and not re.search(r'\b[A-Z][a-z]+\.$', line):
        return False
    return bool(_JOURNAL.search(line))


def _norm(line):
    return re.sub(r'\d+', '#', re.sub(r'\s+', ' ', line.strip().lower()))


def strip_running_headers(pages):
    """Remove repeated header/footer lines and bare page numbers from pages 2+. Page 1 is left intact (its banner and title
    are the identification evidence). Returns (pages, [representative header lines])."""
    if len(pages) < 3:
        return pages, []
    counts = Counter()
    originals = {}
    for text in pages:
        lines = [l for l in text.split('\n') if l.strip()]
        for line in lines[:3] + lines[-3:]:
            if 3 <= len(line.strip()) <= 140:
                key = _norm(line)
                originals.setdefault(key, line.strip())
        for key in {_norm(l) for l in lines[:3] + lines[-3:] if 3 <= len(l.strip()) <= 140}:
            counts[key] += 1
    threshold = max(3, int(0.5 * len(pages)))
    repeated = {k for k, c in counts.items() if c >= threshold}
    cleaned = []
    for number, text in enumerate(pages, 1):
        lines = text.split('\n')
        if number == 1:
            cleaned.append(text)
            continue
        indexes = [i for i, l in enumerate(lines) if l.strip()]
        edge = set(indexes[:3] + indexes[-3:])
        kept = [l for i, l in enumerate(lines) if not (i in edge and (_norm(l) in repeated or re.fullmatch(r'\s*\d{1,4}\s*', l)))]
        cleaned.append('\n'.join(kept))
    return cleaned, [originals[k] for k in repeated if k in originals]


def _title_from_blocks(blocks, body_size, header_norms):
    usable = [b for b in blocks if len(b['text']) >= 8 and not _NOISE.search(b['text']) and _norm(b['text']) not in header_norms]
    sizes = [b['size'] for b in blocks if len(b['text']) > 40]
    body = body_size or (statistics.median(sizes) if sizes else 0)
    candidates = [b for b in usable if not journal_like(b['text'])]
    if not candidates or not body:
        return None, 0
    top = max(b['size'] for b in candidates)
    if top < body * 1.15:
        return None, top                                  # no visibly larger text: the font gives no information
    run, started = [], False
    for block in blocks:
        ok = block in candidates and abs(block['size'] - top) <= 0.6
        if ok:
            run.append(block['text'])
            started = True
        elif started:
            break
    if not run:
        return None, top
    title = re.sub(r'\s+', ' ', ' '.join(run)).strip(' .')
    return (title if 3 <= len(title.split()) <= 30 else None), top


def front_matter(pages, meta, headers):
    """Title, authors, journal and DOI from the first pages. Anything not found is None."""
    from . import docqa
    first = pages[0] if pages else ''
    blocks = (meta or {}).get('blocks') or []
    header_norms = {_norm(h) for h in headers}
    title, authors = None, None
    title_size = 0
    if blocks:
        title, title_size = _title_from_blocks(blocks, (meta or {}).get('body_size') or 0, header_norms)
    if title:
        seen_title, collected = False, []
        for block in blocks:
            text = block['text']
            if title_size and abs(block['size'] - title_size) <= 0.6 and text.strip(' .') and text.strip(' .') in title:
                seen_title = True                       # a line of the title itself, however much it looks like a name list
                continue
            if not seen_title:
                continue
            if re.match(r'(?i)^(abstract|summary|keywords?)\b', text):
                break
            if docqa._looks_like_authors(text):
                collected.append(text)
            elif collected:
                break
        if collected:
            authors = docqa._clean_authors(' '.join(collected))
    journal = _journal(first, blocks, headers, meta or {}, title)
    doi = find_doi('\n'.join(pages[:2])) or find_doi('\n'.join(pages[-1:]))
    return {'title': title, 'authors': authors, 'journal': journal, 'doi': doi}


def _journal(first, blocks, headers, meta, title):
    def tidy(text):
        text = re.sub(r'\b(?:19|20)\d{2}\b|\b\d+\b', ' ', text)
        text = re.sub(r'[|,;:\-\u2013]+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text.title() if text.isupper() else text
    for header in headers:
        if journal_like(header) and not (title and header.strip().lower() in title.lower()):
            return tidy(header)
    lines = [b['text'] for b in blocks[:14]] or [l.strip() for l in first.split('\n') if l.strip()][:14]
    for line in lines:
        if journal_like(line) and not (title and line.lower() in title.lower()) and not _NOISE.search(line):
            return tidy(line)
    match = re.search(r'\b([A-Z][A-Za-z.&]*(?: [A-Z&][A-Za-z.&]*){1,5})\s+(?:19|20)\d{2},\s*\d{1,4}\b', first)
    if match and journal_like(match.group(1)):
        return tidy(match.group(1))
    subject = (meta.get('subject') or '').strip()
    return subject if subject and journal_like(subject) else None


def entities(text):
    """Acronyms and formulas (PDMS, LCN, TiO2, ...) with a plural 's' removed."""
    found = set()
    for token in _ENTITY.findall(text or ''):
        if len(token) >= 3 and token[-1] == 's' and token[:-1].upper() == token[:-1]:
            token = token[:-1]
        found.add(token)
    return found


def captions(pages):
    """Figure, table and scheme captions with their page: [{'kind','label','text','page'}]."""
    found, seen = [], set()
    for number, text in enumerate(pages, 1):
        lines = [l.strip() for l in text.split('\n')]
        for i, line in enumerate(lines):
            match = _CAPTION.match(line)
            if not match:
                continue
            body = match.group(3)
            nxt = i + 1
            while nxt < len(lines) and len(body) < 320 and lines[nxt] and not _CAPTION.match(lines[nxt]) and not body.endswith('.'):
                body += ' ' + lines[nxt]
                nxt += 1
            kind = 'table' if match.group(1).lower().startswith('table') else 'scheme' if match.group(1).lower().startswith(('scheme', 'chart')) else 'figure'
            key = (kind, match.group(2).lower())
            if key in seen:
                continue
            seen.add(key)
            found.append({'kind': kind, 'label': f'{match.group(1).rstrip(".").title()} {match.group(2)}', 'text': re.sub(r'\s+', ' ', body)[:320], 'page': number})
    return found


def tables(pages, caps):
    """Rows that follow a table caption (lines with several numbers), verbatim: [{'label','page','rows'}]."""
    out = []
    for cap in caps:
        if cap['kind'] != 'table':
            continue
        lines = [l.strip() for l in pages[cap['page'] - 1].split('\n')]
        start = next((i for i, l in enumerate(lines) if _CAPTION.match(l) and cap['label'].lower().split()[-1] in l.lower()), None)
        if start is None:
            continue
        rows = []
        for line in lines[start + 1:start + 22]:
            if _CAPTION.match(line):
                break
            if len(re.findall(r'[-+]?\d+(?:\.\d+)?', line)) >= 2 and len(line) <= 160:
                rows.append(line)
        if rows:
            out.append({'label': cap['label'], 'page': cap['page'], 'rows': rows[:14]})
    return out


def equations(pages, limit=30):
    found = []
    for number, text in enumerate(pages, 1):
        for line in text.split('\n'):
            line = line.strip()
            if not 6 <= len(line) <= 140 or '=' not in line:
                continue
            numbered = re.search(r'[\(\[]\d{1,3}[\)\]]\s*$', line)
            math_chars = sum(line.count(c) for c in '=+-\u2212\u00d7\u00f7\u2211\u222b\u221a\u2248\u2264\u2265\u2202\u2207\u00b5\u03bc\u03c3\u03c1\u03bb\u03b1\u03b2\u03b3\u03b4\u03b5\u03c9\u03c0/^_')
            if numbered or (math_chars >= 3 and len(line.split()) <= 12):
                found.append({'page': number, 'text': line})
                if len(found) >= limit:
                    return found
    return found


def sections(chunks):
    """Outline from body chunks: [{'heading','role','pages':[first,last],'chunks':[indexes]}], contiguous chunks merged."""
    out = []
    for chunk in chunks:
        if chunk.is_reference:
            continue
        heading = chunk.section or 'Front matter'
        if out and out[-1]['heading'] == heading:
            out[-1]['chunks'].append(chunk.index)
            out[-1]['pages'][1] = chunk.page
        else:
            kind = role(chunk.section) or ('' if not chunk.section else 'topic')
            out.append({'heading': heading, 'role': kind, 'pages': [chunk.page, chunk.page], 'chunks': [chunk.index]})
    return out


def analyse(pages, chunks):
    caps = captions(pages)
    return {'captions': caps, 'tables': tables(pages, caps), 'equations': equations(pages), 'sections': sections(chunks)}
