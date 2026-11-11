"""Synthetic documents for the docqa regression tests.

They imitate the structure of real papers (front matter, numbered sections, a reference list) without reproducing any
real paper's text. The review fixture deliberately lists, in its references, a paper on a different topic (cilia-like
photoresponsive fibres) - the exact situation in which Sage once summarised the wrong paper.
"""
import textwrap

LCP_TITLE = 'Unlocking the world of material intelligence with liquid crystal polymers'
LCP_AUTHORS = 'Pengrong Lyu, Mert O. Astam and Danqing Liu'
CILIA_TITLE = 'Photoresponsive Fiber Array: Toward Mimicking the Collective Motion of Cilia for Transport Applications'


def _wrap(text, width=88):
    return textwrap.wrap(text, width)


def _paginate(blocks, lines_per_page=44):
    """blocks: list of (kind, text). kind 'h' = heading line, 'p' = wrapped paragraph, 'l' = literal line."""
    lines = []
    for kind, text in blocks:
        if kind == 'p':
            lines.extend(_wrap(text))
            lines.append('')
        else:
            lines.append(text)
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)]
    return pages


def _filler(section, count):
    materials = ['main-chain', 'side-chain', 'smectic', 'nematic', 'cholesteric', 'azobenzene-doped']
    methods = ['spin coating', 'photolithography', 'extrusion', 'casting', 'mould replication', 'thermal annealing']
    properties = ['stiffness', 'response speed', 'optical transparency', 'swelling behaviour', 'thermal stability', 'processability']
    paragraphs = []
    for i in range(count * 3):
        m, meth, prop = materials[i % 6], methods[(i + section) % 6], properties[(i * 2 + section) % 6]
        paragraphs.append(
            f'Several groups have examined {m} networks prepared by {meth}, and they describe a {prop} that depends on the '
            f'crosslink density and on the processing history of the sample. Comparisons between laboratories remain difficult '
            f'because the reported networks differ in composition, in thickness and in the way the {prop} was quantified. '
            f'Still, the trend described for {m} systems is consistent across the studies discussed in this section.')
    return [('p', t) for t in paragraphs]


def lcp_review_pages():
    blocks = [
        ('l', 'Review Article'),
        ('l', LCP_TITLE),
        ('l', LCP_AUTHORS),
        ('l', 'Received 3 March 2025; accepted 28 May 2025; published 2025'),
        ('l', 'Abstract'),
        ('p', 'This review summarises recent progress in liquid crystal polymers and liquid crystal elastomers as materials that '
              'sense, process and respond to their environment. We discuss molecular design, alignment strategies, fabrication '
              'techniques such as inkjet printing and direct ink writing, and stimuli-responsive actuation driven by light and '
              'heat. We highlight challenges in scalability, fatigue and integration, and outline directions for future research.'),
        ('l', 'Keywords: liquid crystal polymers, actuators, material intelligence, soft robotics'),
        ('l', '1. Introduction'),
        ('p', 'Material intelligence describes materials whose structure encodes sensing, computation and actuation. Liquid crystal '
              'polymers combine molecular anisotropy with rubber elasticity, which lets a network change shape reversibly when the '
              'order of its mesogens changes. Reported actuation strains of up to 40% have been achieved in aligned networks, as '
              'discussed in earlier work.'),
    ] + _filler(1, 3) + [
        ('l', '2. Molecular design of liquid crystal polymers'),
        ('p', 'The choice of mesogen, spacer and crosslinker sets the transition temperatures and the stiffness of a network. '
              'A nematic to isotropic transition temperature of 85 \u00b0C is typical for acrylate networks described in earlier studies. '
              'Lower crosslink density increases the achievable strain but reduces the blocking force.'),
    ] + _filler(2, 3) + [
        ('l', '3. Alignment and processing'),
        ('p', 'Uniform alignment is required for predictable actuation. Surface alignment with rubbed polyimide layers, magnetic '
              'fields and shear flow during direct ink writing and inkjet printing are the main routes covered here. Printed '
              'filaments with diameters of 50 \u00b5m have been reported, and the printing path then programs the local director.'),
    ] + _filler(3, 3) + [
        ('l', '4. Stimuli-responsive actuation'),
        ('p', 'Light can drive actuation through azobenzene photoisomerisation or through photothermal dyes, while heat acts '
              'directly on the order parameter. Cycle lifetimes beyond 10000 cycles have been shown for selected systems, '
              'although most reports stop after far fewer cycles.'),
    ] + _filler(4, 3) + [
        ('l', '5. Applications'),
        ('p', 'Soft robotics, haptic interfaces, adaptive optics and smart textiles are the application areas that are discussed. '
              'In each area the review compares what has been demonstrated with what is still conceptual.'),
    ] + _filler(5, 3) + [
        ('l', '6. Challenges and outlook'),
        ('p', 'Scaling up synthesis, fatigue under repeated cycling, integration with electronics and standardised characterisation '
              'remain open challenges. The authors propose that machine-learning-guided design and multi-stimuli networks could be '
              'explored in future research.'),
        ('l', '7. Conclusions'),
        ('p', 'Liquid crystal polymers are a versatile platform for material intelligence, but practical adoption depends on '
              'solving the scalability and durability issues outlined above.'),
        ('l', 'References'),
    ]
    refs = [
        '[1] Warner, M.; Terentjev, E. M. Liquid Crystal Elastomers. Oxford University Press 2003.',
        '[2] White, T. J.; Broer, D. J. Programmable and adaptive mechanics with liquid crystal polymer networks. Nat. Mater. 2015, 14, 1087.',
        f'[3] Gelebart, A. H.; Liu, D.; Mulder, D. J.; et al. {CILIA_TITLE}. Adv. Funct. Mater. 2016, 26, 5322. PANI/PDA fibers.',
        '[4] Ware, T. H.; McConney, M. E.; Wie, J. J.; Tondiglia, V. P.; White, T. J. Voxelated liquid crystal elastomers. Science 2015, 347, 982.',
        '[5] Kotikian, A.; Truby, R. L.; Boley, J. W.; White, T. J.; Lewis, J. A. 3D printing of liquid crystal elastomeric actuators. Adv. Mater. 2018, 30, 1706164.',
        '[6] Ohm, C.; Brehmer, M.; Zentel, R. Liquid crystalline elastomers as actuators and sensors. Adv. Mater. 2010, 22, 3366.',
        '[7] Yu, Y.; Nakano, M.; Ikeda, T. Directed bending of a polymer film by light. Nature 2003, 425, 145.',
        '[8] Ahn, C.; Liang, X.; Cai, S. Bioinspired design of light-powered crawling. Adv. Intell. Syst. 2019, 1, 1900051.',
    ]
    pages = _paginate(blocks)
    pages.append(refs)
    return pages


def graphene_epoxy_pages():
    blocks = [
        ('l', 'Research Article'),
        ('l', 'Thermal conductivity of graphene-filled epoxy composites'),
        ('l', 'Anna Kowalski, Jan de Vries and Mei Tanaka'),
        ('l', 'Received 12 January 2024; accepted 2 April 2024'),
        ('l', 'Abstract'),
        ('p', 'In this work we prepared epoxy composites filled with graphene nanoplatelets and measured their thermal and '
              'mechanical properties. Thermal conductivity increased strongly with filler loading while tensile strength '
              'decreased, which limits the useful loading range.'),
        ('l', 'Keywords: epoxy, graphene, thermal conductivity'),
        ('l', '1. Introduction'),
        ('p', 'Heat dissipation limits the power density of electronics. Epoxy resins are attractive encapsulants but conduct '
              'heat poorly. Graphene nanoplatelets are a candidate filler because of their high in-plane conductivity.'),
        ('l', '2. Experimental'),
        ('p', 'Composites were prepared at filler loadings of 5, 10, 20 and 30 wt% by mixing graphene nanoplatelets into a '
              'bisphenol-A epoxy and curing at 120 \u00b0C for 2 h. Thermal conductivity was measured by laser flash analysis at '
              '25 \u00b0C. Tensile tests followed a standard dog-bone geometry.'),
        ('l', '3. Results and discussion'),
        ('p', 'Thermal conductivity rose from 0.21 W/mK for neat epoxy to 1.84 W/mK at 20 wt% loading. Tensile strength fell '
              'from 62 MPa for the neat resin to 48 MPa at the same loading. The glass transition temperature was 118 \u00b0C for '
              'the neat resin and changed little with filler content.'),
        ('p', 'The 30 wt% samples contained visible agglomerates, and their conductivity was lower than that of the 20 wt% '
              'samples.'),
        ('l', '4. Limitations'),
        ('p', 'A limitation of this study is that only one filler size was tested and that ageing was not examined.'),
        ('l', '5. Conclusions'),
        ('p', 'A loading of about 20 wt% gives the best compromise between thermal conductivity and strength in this system.'),
        ('l', 'References'),
    ]
    refs = [
        '[1] Balandin, A. A. Superior thermal conductivity of single-layer graphene. Nano Lett. 2008, 8, 902.',
        '[2] Shahil, K. M. F.; Balandin, A. A. Graphene-multilayer graphene nanocomposites as highly efficient thermal interface materials. Nano Lett. 2012, 12, 861.',
        '[3] Song, S. H.; et al. Enhanced thermal conductivity of epoxy-graphene composites. Carbon 2014, 66, 90.',
        '[4] Teng, C.-C.; et al. Thermal conductivity and structure of non-covalent functionalized graphene/epoxy composites. Carbon 2011, 49, 5107.',
    ]
    pages = _paginate(blocks)
    pages.append(refs)
    return pages


def lc_display_pages():
    blocks = [
        ('l', 'Research Article'),
        ('l', 'Liquid crystal display cells: effect of alignment layer rubbing on switching speed'),
        ('l', 'Sofia Marques, Tom Vermeulen and Ravi Patel'),
        ('l', 'Received 8 June 2023; accepted 15 August 2023'),
        ('l', 'Abstract'),
        ('p', 'We fabricated twisted nematic liquid crystal display cells with different rubbing strengths of the polyimide '
              'alignment layer and measured their electro-optic switching speed.'),
        ('l', 'Keywords: liquid crystal display, alignment layer, response time'),
        ('l', '1. Introduction'),
        ('p', 'The response time of a liquid crystal display depends on the viscosity of the liquid crystal, the cell gap and the '
              'anchoring strength of the alignment layer.'),
        ('l', '2. Experimental'),
        ('p', 'Twisted nematic cells with a cell gap of 5 \u00b5m were assembled from glass substrates coated with rubbed polyimide. '
              'Rubbing strength was varied by the number of passes of the velvet cloth.'),
        ('l', '3. Results'),
        ('p', 'The rise time decreased from 21 ms to 12 ms at a drive voltage of 5 V when the rubbing strength was doubled. '
              'The contrast ratio stayed above 500:1 for all cells.'),
        ('l', '4. Conclusions'),
        ('p', 'Stronger rubbing improves the switching speed of twisted nematic cells without reducing contrast.'),
        ('l', 'References'),
    ]
    refs = [
        '[1] Schadt, M.; Helfrich, W. Voltage-dependent optical activity of a twisted nematic liquid crystal. Appl. Phys. Lett. 1971, 18, 127.',
        '[2] Yeh, P.; Gu, C. Optics of Liquid Crystal Displays. Wiley 1999.',
        '[3] Wu, S.-T.; Yang, D.-K. Fundamentals of Liquid Crystal Devices. Wiley 2006.',
        '[4] Nehring, J.; Kmetz, A. R.; Scheffer, T. J. Analysis of weak-boundary-coupling effects. J. Appl. Phys. 1976, 47, 850.',
    ]
    pages = _paginate(blocks)
    pages.append(refs)
    return pages


def pdf_bytes(pages):
    """A minimal multi-page text PDF. pages: list of list of lines (ASCII/Latin-1)."""
    def esc(text):
        return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    objects = [b'<</Type/Catalog/Pages 2 0 R>>', None]
    kids = []
    for lines in pages:
        content = ['BT /F1 9 Tf 12 LEADING TL 40 780 Td'.replace('LEADING', '12')]
        for line in lines:
            content.append(f'({esc(line)}) Tj T*')
        content.append('ET')
        stream = '\n'.join(content).encode('latin-1', 'replace')
        page_number = len(objects) + 1
        content_number = page_number + 1
        kids.append(f'{page_number} 0 R')
        objects.append(f'<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 800]/Contents {content_number} 0 R/Resources<</Font<</F1 FONT 0 R>>>>>>'.encode())
        objects.append(b'<</Length %d>>\nstream\n' % len(stream) + stream + b'\nendstream')
    font_number = len(objects) + 1
    objects.append(b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>')
    objects[1] = f'<</Type/Pages/Kids[{" ".join(kids)}]/Count {len(kids)}>>'.encode()
    objects = [o.replace(b'FONT 0 R', f'{font_number} 0 R'.encode()) if o else o for o in objects]
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
