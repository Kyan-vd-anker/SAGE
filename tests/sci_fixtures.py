"""Layout-aware PDF fixtures for the scientific-reading tests.

journal_review_pdf() builds a small journal-style review: a bold journal banner, a large two-line title, authors, DOI, a
full-width abstract, three two-column pages with bold section headings, running headers and page numbers, a table, a
figure caption and a numbered equation, then a reference page. The columns are drawn row by row (left line, right line), so
a naive extraction interleaves them - exactly the damage the layout-aware reader must undo.
"""
import textwrap

TITLE_1 = 'Liquid Crystal Networks and Liquid Crystal Oligomer Networks:'
TITLE_2 = 'Molecular Design, Processing and Actuation'
TITLE = f'{TITLE_1} {TITLE_2}'
AUTHORS = 'Jan de Vries, Anna Kowalski and Mei Tanaka'
JOURNAL = 'Advanced Functional Materials'
DOI = '10.1002/adfm.202500123'

ABSTRACT = ('This review compares liquid crystal networks (LCNs) with liquid crystal oligomer networks (LCONs) as soft actuator '
            'materials. We discuss how molecular design, crosslinking chemistry and alignment determine modulus, transition '
            'temperature and actuation strain, and we outline the challenges that remain for processing and durability.')

COLUMNS = [
    # page 2 (left column, right column)
    (['1. Introduction',
      'Soft actuators change shape in response to heat or light. Liquid crystal networks (LCNs) are formed by photopolymerisation of '
      'reactive mesogens such as RM257 together with a chain extender. Their stiff, densely crosslinked structure gives a high modulus '
      'and a sharp response. zzLeftEndPageTwozz'],
     ['The molecular order of the mesogens sets the direction of the actuation. zzRightStartPageTwozz Alignment is imposed by rubbing '
      'a polyimide layer or by photoalignment before the network is fixed. Figure 1 sketches the two routes.',
      'Figure 2. Alignment of mesogens by rubbing and photoalignment.']),
    # page 3
    (['2. Liquid Crystal Oligomer Networks',
      'Liquid crystal oligomer networks (LCONs) are formed by a Michael addition between diacrylate mesogens and a dithiol, which '
      'first builds oligomers that are crosslinked in a second step. The longer flexible segments lower the modulus and the glass '
      'transition temperature. zzLeftEndPageThreezz',
      'The relation between stress and strain is described by the linear law', 'sigma = E epsilon   (1)'],
     ['zzRightStartPageThreezz Because the oligomers are chain extended, LCON films reach a conductivity of 1.2 x 10-3 S/m when doped '
      'with TiO2 at 25 \u00b0C, and they stay flexible well below room temperature.',
      'Table 1. Properties of LCN and LCON films.', 'Network Modulus Tg Strain', 'LCN 85 MPa 45 \u00b0C 12 %', 'LCON 1.2 MPa 18 \u00b0C 55 %']),
    # page 4
    (['3. Comparison',
      'The LCN films are stiff and fast but fracture at small strain, whereas the LCON films are soft and reach large reversible '
      'strain at a much lower blocking force. zzLeftEndPageFourzz'],
     ['zzRightStartPageFourzz The choice therefore depends on whether force or strain matters more for the application.',
      'Limitations', 'Both families lose actuation strain after repeated cycling, and scalable alignment of thick films remains '
      'an open problem for processing.']),
]


def _wrap(text, width):
    return textwrap.wrap(text, width)


def _esc(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def layout_pdf(pages, width=612, height=800):
    """pages: list of lists of (text, x, y, size, bold). Returns PDF bytes."""
    objects = [b'<</Type/Catalog/Pages 2 0 R>>', None]
    kids = []
    for items in pages:
        stream = '\n'.join(f"BT /{'F2' if bold else 'F1'} {size} Tf 1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj ET" for text, x, y, size, bold in items)
        stream = stream.encode('latin-1', 'replace')
        page_number = len(objects) + 1
        kids.append(f'{page_number} 0 R')
        objects.append((f'<</Type/Page/Parent 2 0 R/MediaBox[0 0 {width} {height}]/Contents {page_number + 1} 0 R'
                        f'/Resources<</Font<</F1 FONT1 0 R/F2 FONT2 0 R>>>>>>').encode())
        objects.append(b'<</Length %d>>\nstream\n' % len(stream) + stream + b'\nendstream')
    font1, font2 = len(objects) + 1, len(objects) + 2
    objects.append(b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>')
    objects.append(b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica-Bold/Encoding/WinAnsiEncoding>>')
    objects[1] = f'<</Type/Pages/Kids[{" ".join(kids)}]/Count {len(kids)}>>'.encode()
    objects = [o.replace(b'FONT1 0 R', f'{font1} 0 R'.encode()).replace(b'FONT2 0 R', f'{font2} 0 R'.encode()) if o else o for o in objects]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    out += b'trailer\n<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


def _column_items(blocks, x, top=730):
    """Wrap text blocks into lines at 9 pt; a block that is exactly a heading (starts with a digit+dot or is 'Limitations') is bold 11 pt."""
    items, y = [], top
    for block in blocks:
        heading = bool(block[:2].strip('. ').isdigit() and block[1:3].startswith('.')) or block == 'Limitations'
        if heading:
            items.append((block, x, y, 11, True))
            y -= 16
            continue
        for line in _wrap(block, 50):
            items.append((line, x, y, 9, False))
            y -= 12
        y -= 4
    return items


def _two_column_page(number, left_blocks, right_blocks):
    left, right = _column_items(left_blocks, 40), _column_items(right_blocks, 320)
    items = [(JOURNAL, 40, 775, 8, False), (str(number), 300, 30, 8, False)]
    for i in range(max(len(left), len(right))):                       # drawn row by row: plain extraction interleaves the columns
        if i < len(left):
            items.append(left[i])
        if i < len(right):
            items.append(right[i])
    return items


def journal_review_pages():
    first = [(JOURNAL, 40, 760, 16, True), ('www.afm-journal.de', 380, 765, 8, False), ('REVIEW', 40, 735, 12, False),
             (TITLE_1, 40, 700, 20, True), (TITLE_2, 40, 674, 20, True), (AUTHORS, 40, 645, 11, False),
             (f'DOI: {DOI}', 40, 625, 8, False), ('Received: 4 February 2025; Published online: 20 May 2025', 40, 612, 8, False),
             ('Abstract', 40, 585, 12, True)]
    y = 565
    for line in _wrap(ABSTRACT, 105):
        first.append((line, 40, y, 9, False))
        y -= 12
    pages = [first]
    for number, (left, right) in enumerate(COLUMNS, 2):
        pages.append(_two_column_page(number, left, right))
    refs = [(JOURNAL, 40, 775, 8, False), ('5', 300, 30, 8, False), ('References', 40, 730, 11, True)]
    entries = ['[1] Warner, M.; Terentjev, E. M. Liquid Crystal Elastomers. Oxford University Press 2003.',
               '[2] White, T. J.; Broer, D. J. Programmable and adaptive mechanics with liquid crystal polymer networks. Nat. Mater. 2015, 14, 1087.',
               '[3] Ware, T. H.; et al. Voxelated liquid crystal elastomers. Science 2015, 347, 982.',
               '[4] Yu, Y.; Nakano, M.; Ikeda, T. Directed bending of a polymer film by light. Nature 2003, 425, 145.']
    y = 710
    for entry in entries:
        for line in _wrap(entry, 95):
            refs.append((line, 40, y, 9, False))
            y -= 12
    pages.append(refs)
    return pages


def journal_review_pdf():
    return layout_pdf(journal_review_pages())


def scanned_pdf(n=3):
    """Pages that contain no text at all (an image-only scan)."""
    return layout_pdf([[] for _ in range(n)])


def partly_empty_pdf():
    pages = journal_review_pages()
    pages[3] = []                                        # one body page without extractable text
    return layout_pdf(pages)
