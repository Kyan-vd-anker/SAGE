"""Layout-aware text extraction and quality checks for scientific PDFs.

pypdf's plain text extraction follows the order of the PDF's content stream. For two-column journal articles that interleaves
the columns, and it throws away font sizes, so a journal banner and the real title look the same. This module

  * asks pypdf for its layout-mode text (which keeps the two columns side by side), finds the gutter and reads the page column
    by column, full-width blocks (title, abstract, wide tables) staying where they are,
  * asks pypdf for every text fragment with its font size and weight and attaches them to the lines, so large and bold lines
    (the title, section headings) can be recognised,
  * repairs extraction artefacts (ligatures, letter-spaced words, stray spaces), keeping formulas, units and scientific
    notation untouched, and
  * measures how trustworthy the result is, so that scanned or garbled pages are reported instead of silently summarised.

Positions reported by pypdf's text callback are not used: they are unreliable across pypdf versions and PDF producers. Every
step falls back to plain extraction when it fails, and a page whose rebuilt text lost most of the plain text keeps the plain text.
"""
import io
import math
import re
import statistics
from collections import Counter

_LIGATURES = {'\ufb00': 'ff', '\ufb01': 'fi', '\ufb02': 'fl', '\ufb03': 'ffi', '\ufb04': 'ffl', '\ufb05': 'st', '\ufb06': 'st',
              '\u00a0': ' ', '\u2009': ' ', '\u202f': ' ', '\u2002': ' ', '\u2003': ' ', '\u200b': '', '\ufeff': ''}
_SPACED_LETTERS = re.compile(r'(?<![\w])(?:[A-Za-z] ){4,}[A-Za-z](?![\w])')
_REPLACEMENT = re.compile('[\ufffd\ue000-\uf8ff]')


def repair_text(text):
    """Undo common extraction damage without touching scientific content (formulas, units, exponents, Greek letters)."""
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(' ', ''), text)           # 'L i q u i d' -> 'Liquid'
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return text


def quality_report(pages):
    """-> {'warnings': [...], 'severe': bool, 'empty_pages': [...], 'garbled_pages': [...]}. Page numbers are 1-based."""
    empty, garbled = [], []
    for number, text in enumerate(pages, 1):
        compact = re.sub(r'\s+', '', text)
        if len(compact) < 40:
            empty.append(number)
            continue
        tokens = text.split()
        single = sum(1 for t in tokens if len(t) == 1 and t.isalpha()) / max(1, len(tokens))
        alpha = sum(c.isalpha() for c in compact) / len(compact)
        broken = len(_REPLACEMENT.findall(text)) / len(compact)
        if broken > 0.04 or single > 0.45 or alpha < 0.35:
            garbled.append(number)
    total = max(1, len(pages))
    warnings = []
    if empty:
        warnings.append(f'No text could be extracted from page{"s" if len(empty) > 1 else ""} {_ranges(empty)} (figures, scans or images).')
    if garbled:
        warnings.append(f'The text on page{"s" if len(garbled) > 1 else ""} {_ranges(garbled)} looks garbled (broken characters or spacing); '
                        'statements about those pages may be unreliable.')
    severe = (len(empty) + len(garbled)) / total >= 0.5
    return {'warnings': warnings, 'severe': severe, 'empty_pages': empty, 'garbled_pages': garbled}


def _ranges(numbers):
    out, start, prev = [], numbers[0], numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f'{start}-{prev}')
        if n is not None:
            start = prev = n
    return ', '.join(out)


def _effective_size(font_size, cm, tm):
    scale = math.hypot(tm[2], tm[3]) * math.hypot(cm[2], cm[3]) if len(tm) >= 4 and len(cm) >= 4 else 1.0
    size = (font_size or 0) * (scale or 1.0)
    return round(size if size > 0.5 else (font_size or 10.0), 1)


def split_columns(layout_text):
    """Read a pypdf layout-mode page column by column. -> (lines, two_columns). A page is two-column when many lines carry a
    wide blank gap at the same horizontal position (the gutter); lines that cross the gutter are full-width and stay in place."""
    raw = [l.rstrip() for l in layout_text.replace('\t', ' ').split('\n')]
    lines = []
    for l in raw:                                                  # layout mode pads pages with blank lines; keep at most one
        if l.strip() or (lines and lines[-1].strip()):
            lines.append(l)
    body = [l for l in lines if len(l.strip()) >= 30]
    hist = Counter()
    for l in body:
        for run in re.finditer(r'(?<=\S) {4,}(?=\S)', l):
            for col in range(run.start(), run.end()):
                hist[col] += 1
    if not hist:
        return [re.sub(r'\s+', ' ', l).strip() for l in lines if l.strip()], False
    gutter, support = hist.most_common(1)[0]
    if support < 4 or support < 0.4 * len(body):
        return [re.sub(r'\s+', ' ', l).strip() for l in lines if l.strip()], False
    left, right, ordered = [], [], []

    def flush():
        ordered.extend(left)
        ordered.extend(right)
        left.clear()
        right.clear()

    for l in lines:
        if not l.strip():
            continue
        run = next((r for r in re.finditer(r'(?<=\S) {3,}(?=\S)', l) if r.start() <= gutter < r.end() + 2), None)
        stripped = l.lstrip()
        indent = len(l) - len(stripped)
        if run:
            a, b = l[:run.start()].strip(), l[run.end():].strip()
            left.append(re.sub(r'\s+', ' ', a))
            right.append(re.sub(r'\s+', ' ', b))
        elif indent >= gutter - 2:
            right.append(re.sub(r'\s+', ' ', stripped))
        elif len(l.rstrip()) <= gutter:
            left.append(re.sub(r'\s+', ' ', stripped))
        else:
            flush()
            ordered.append(re.sub(r'\s+', ' ', stripped))
    flush()
    return ordered, True


def _annotate(lines, fragments):
    """Attach a font size and a bold flag to each line, taken from the fragments whose text it contains."""
    out = []
    usable = [(re.sub(r'\s+', ' ', t).strip(), s, b) for t, s, b in fragments if len(t.strip()) >= 3]
    for text in lines:
        sizes, bolds = [], []
        for frag, size, bold in usable:
            if frag and frag in text:
                sizes.append(size)
                bolds.append(bold)
        out.append({'text': text, 'size': max(sizes) if sizes else 0.0, 'bold': bool(bolds) and all(bolds)})
    return out


def extract(data, max_pages=500):
    """-> (pages, meta). `meta` holds the PDF's embedded title/author/created/subject, the font-sized lines of page 1 ('blocks'),
    'body_size', 'headings' (large or bold short lines from every page), 'columns' (pages read as two columns) and 'layout'
    (False when only plain extraction was possible)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > max_pages:
        raise ValueError(f'PDF has more than {max_pages} pages')
    meta = {}
    try:
        info = reader.metadata
        if info:
            meta = {'title': str(info.title or ''), 'author': str(info.author or ''), 'created': str(info.get('/CreationDate') or ''),
                    'subject': str(info.subject or '')}
    except Exception:
        meta = {}
    pages, columns, annotated = [], [], []
    for number, page in enumerate(reader.pages, 1):
        fragments = []

        def visitor(text, cm, tm, font_dict, font_size, fragments=fragments):
            if not text or not text.strip():
                return
            try:
                name = str(font_dict.get('/BaseFont', '')) if hasattr(font_dict, 'get') else ''
                size = _effective_size(font_size, tm if len(tm) >= 6 else [1, 0, 0, 1, 0, 0], cm if len(cm) >= 6 else [1, 0, 0, 1, 0, 0])
                fragments.append((text, size, bool(re.search(r'(?i)bold|black|heavy|semibold|demi', name))))
            except Exception:
                pass

        try:
            plain = page.extract_text(visitor_text=visitor) or ''
        except Exception:
            try:
                plain = page.extract_text() or ''
            except Exception:
                plain = ''
            fragments = []
        text, lines, two = plain, [l.strip() for l in plain.split('\n') if l.strip()], False
        try:
            layout = page.extract_text(extraction_mode='layout')
        except Exception:                                      # old pypdf without layout mode, or a page it cannot lay out
            layout = None
        if layout and layout.strip():
            ordered, two = split_columns(layout)
            rebuilt = '\n'.join(ordered)
            # a rebuilt page that lost most of the plain text means the layout pass was unusable: keep the plain text
            if len(re.sub(r'\s+', '', rebuilt)) >= 0.6 * len(re.sub(r'\s+', '', plain)):
                text, lines = rebuilt, ordered
            else:
                two = False
        pages.append(text)
        columns.append(two)
        if number == 1 and layout and layout.strip() and not two:
            # front matter: split a layout row at wide gaps, so the journal banner and a web address on the same row are separate lines
            front = [seg.strip() for row in layout.split('\n') for seg in re.split(r' {5,}', row) if seg.strip()]
            lines = front if len(re.sub(r'\s+', '', ' '.join(front))) >= 0.6 * len(re.sub(r'\s+', '', plain)) else lines
        annotated.append((number, _annotate(lines, fragments)))
    sizes = [l['size'] for _, lines in annotated for l in lines if len(l['text']) > 40 and l['size']]
    body = statistics.median(sizes) if sizes else 0
    headings = set()
    for number, lines in annotated:
        start = 0
        if number == 1:                                    # the title block, authors and journal banner are not section headings
            start = next((i for i, l in enumerate(lines) if re.match(r'(?i)^(abstract|summary|keywords?|1\.?\s+introduction)\b', l['text'])), len(lines))
        for line in lines[start:]:
            short = 3 <= len(line['text']) <= 90 and not line['text'].endswith(('.', ',', ';'))
            if short and line['size'] and ((line['bold'] and len(line['text'].split()) <= 12) or (body and line['size'] >= body * 1.12)):
                headings.add(re.sub(r'\s+', ' ', line['text']).strip().lower())
    first = dict(annotated).get(1, [])
    meta['blocks'] = [{'text': l['text'], 'size': l['size'], 'bold': l['bold']} for l in first[:60]]
    meta['body_size'] = body
    meta['headings'] = sorted(headings)
    meta['columns'] = [i + 1 for i, c in enumerate(columns) if c]
    meta['layout'] = any(l['size'] for _, lines in annotated for l in lines)
    return pages, meta
