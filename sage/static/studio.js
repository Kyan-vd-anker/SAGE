/* Sage Studio: LaTeX, document and sheet editors with Sage reading along. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const say = text => { if (typeof toast === 'function') toast(text); };
  const KIND_LABEL = { latex: 'LaTeX', doc: 'Document', sheet: 'Sheet' };
  const EXPORTS = { latex: [['tex', 'LaTeX (.tex)']], doc: [['docx', 'Word (.docx)'], ['md', 'Markdown (.md)']], sheet: [['xlsx', 'Excel (.xlsx)'], ['csv', 'CSV (.csv)']] };
  const RANK = { info: 1, warn: 2, error: 3 };
  const COLUMNS = 26;

  const S = { inited: false, docs: [], doc: null, notes: [], dismissed: new Set(), counter: 0, from: null, to: 0, prevLines: 1,
              saveTimer: null, reviewTimer: null, reviewing: false, cells: {}, values: {}, sel: 'A1', rows: 40, changed: new Set(),
              pdfUrl: null, engine: undefined, room: 'bedroom', dirty: false };

  async function call(url, body) {
    const r = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : { cache: 'no-store' });
    let data = {};
    try { data = await r.json(); } catch { /* empty body */ }
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  }

  // ------------------------------------------------------------------ small helpers

  const every = () => Math.max(1, Math.min(50, parseInt(el('studioEvery').value, 10) || 5));
  const isText = () => S.doc && S.doc.kind !== 'sheet';
  const area = () => el('studioArea');
  const textLines = () => area().value.split('\n');
  const cursorLine = () => area().value.slice(0, area().selectionStart).split('\n').length;
  const place = n => (n.line ? `Line ${n.line}` : n.cell ? `Cell ${n.cell}` : 'Document');

  function visibleNotes() {
    const lines = isText() ? textLines() : [];
    return S.notes.filter(n => !n.snap || n.line === undefined || (lines[n.line - 1] ?? '') === n.snap)
      .filter(n => !S.dismissed.has(`${n.line ?? n.cell ?? ''}:${n.message}`));
  }

  function setSaved(text) { el('studioSaved').textContent = text; }

  // ------------------------------------------------------------------ documents

  async function loadDocs(selectId) {
    S.docs = await call('/api/studio/docs');
    const select = el('studioDocs');
    select.innerHTML = S.docs.length ? S.docs.map(d => `<option value="${d.id}">${esc(KIND_LABEL[d.kind] || d.kind)} · ${esc(d.title)}</option>`).join('') : '<option value="">No documents yet</option>';
    if (selectId) select.value = String(selectId);
  }

  async function openDoc(id) {
    if (S.doc && S.dirty) await save();
    if (!id) { S.doc = null; render(); return; }
    S.doc = await call(`/api/studio/doc/${id}`);
    S.notes = []; S.dismissed = new Set(); S.counter = 0; S.from = null; S.to = 0; S.changed = new Set(); S.dirty = false;
    S.sel = 'A1';
    try { localStorage.setItem('studioLast', String(id)); } catch { /* private mode */ }
    if (S.doc.kind === 'sheet') {
      S.cells = JSON.parse(S.doc.content || '{"cells":{}}').cells || {};
      S.rows = Math.max(40, ...Object.keys(S.cells).map(r => parseInt(r.replace(/^[A-Z]+/, ''), 10) + 10));
      recompute();
    } else {
      area().value = S.doc.content;
      S.prevLines = textLines().length;
    }
    el('studioDocs').value = String(id);
    el('studioTitle').value = S.doc.title;
    setSaved('Saved');
    render();
  }

  async function createDoc(kind, title, content) {
    const doc = await call('/api/studio/doc', { kind, title, content });
    await loadDocs(doc.id);
    await openDoc(doc.id);
  }

  function markDirty() {
    S.dirty = true;
    setSaved('Unsaved changes');
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(() => save().catch(e => setSaved('Could not save: ' + e.message)), 1200);
  }

  async function save() {
    if (!S.doc) return;
    clearTimeout(S.saveTimer);
    const content = S.doc.kind === 'sheet' ? JSON.stringify({ cells: S.cells }) : area().value;
    const title = el('studioTitle').value.trim() || 'Untitled';
    setSaved('Saving…');
    const doc = await call(`/api/studio/doc/${S.doc.id}`, { title, content });
    S.doc.title = doc.title; S.doc.content = doc.content; S.dirty = false;
    setSaved('Saved');
    const option = [...el('studioDocs').options].find(o => o.value === String(S.doc.id));
    if (option) option.textContent = `${KIND_LABEL[S.doc.kind]} · ${doc.title}`;
  }

  // ------------------------------------------------------------------ layout

  function render() {
    const kind = S.doc ? S.doc.kind : null;
    el('studioText').classList.toggle('hidden', !isText());
    el('studioSheet').classList.toggle('hidden', kind !== 'sheet');
    el('studioEmpty').classList.toggle('hidden', !!S.doc);
    el('studioText').classList.toggle('wrap', kind === 'doc');
    el('studioCompile').classList.toggle('hidden', kind !== 'latex');
    el('studioPreviewBtn').classList.toggle('hidden', kind !== 'doc');
    el('studioDelete').disabled = el('studioTitle').disabled = el('studioReview').disabled = el('studioExport').disabled = !S.doc;
    el('studioExport').innerHTML = '<option value="">Export…</option>' + (EXPORTS[kind] || []).map(([f, l]) => `<option value="${f}">${l}</option>`).join('');
    if (kind !== 'latex' && kind !== 'doc') hidePreview();
    if (kind === 'sheet') renderSheet(); else if (isText()) { renderText(); if (el('studioPreview').dataset.mode === 'doc') renderDocPreview(); }
    renderNotes();
  }

  // ------------------------------------------------------------------ text editor (LaTeX and documents)

  function renderText() {
    const lines = textLines();
    const mark = {};
    for (const n of visibleNotes()) if (n.line && (!mark[n.line] || RANK[n.severity] > RANK[mark[n.line]])) mark[n.line] = n.severity;
    el('studioBack').innerHTML = lines.map((t, i) => `<div class="bl ${mark[i + 1] ? 'n-' + mark[i + 1] : ''}">${esc(t) || '&nbsp;'}</div>`).join('');
    el('studioGutter').innerHTML = lines.map((_, i) => `<div class="gl ${mark[i + 1] ? 'n-' + mark[i + 1] : ''}">${i + 1}</div>`).join('');
    syncScroll();
  }

  function syncScroll() {
    const a = area();
    el('studioBack').scrollTop = a.scrollTop; el('studioBack').scrollLeft = a.scrollLeft;
    el('studioGutter').scrollTop = a.scrollTop;
  }

  let paintFrame = 0;
  function paintSoon() { if (!paintFrame) paintFrame = requestAnimationFrame(() => { paintFrame = 0; renderText(); renderNotes(); }); }

  function shiftNotes(afterLine, delta) {
    if (!delta) return;
    if (delta < 0) {
      const gone = -delta;
      S.notes = S.notes.filter(n => !(n.line > afterLine && n.line <= afterLine + gone));
      S.notes.forEach(n => { if (n.line > afterLine + gone) n.line += delta; });
    } else {
      S.notes.forEach(n => { if (n.line > afterLine) n.line += delta; });
    }
  }

  function onTextInput() {
    const lines = textLines().length, delta = lines - S.prevLines, c = cursorLine();
    if (delta > 0) { shiftNotes(c - delta, delta); S.counter += delta; }
    else if (delta < 0) shiftNotes(c, delta);
    S.prevLines = lines;
    const first = Math.max(1, c - Math.max(delta, 0));
    S.from = S.from === null ? first : Math.min(S.from, first);
    S.to = Math.max(S.to, c);
    markDirty();
    paintSoon();
    if (S.doc.kind === 'doc' && el('studioPreview').dataset.mode === 'doc') renderDocPreview();
    if (S.counter >= every()) { clearTimeout(S.reviewTimer); S.reviewTimer = setTimeout(autoReview, 1200); }
  }

  function autoReview() {
    if (!S.doc || S.reviewing) { S.reviewTimer = setTimeout(autoReview, 1500); return; }
    const lines = textLines();
    const c = cursorLine();
    const to = Math.max(S.from || 1, lines[c - 1] && lines[c - 1].trim() ? c : c - 1);
    const focus = { from: S.from || 1, to };
    S.counter = 0; S.from = null; S.to = 0;
    review(focus);
  }

  // ------------------------------------------------------------------ review

  async function review(focus) {
    if (!S.doc || S.reviewing) return;
    S.reviewing = true;
    el('studioReadState').textContent = 'Sage is reading…';
    try {
      const body = { kind: S.doc.kind, speak: el('studioSpeak').value, focus };
      if (S.doc.kind === 'sheet') {
        body.content = JSON.stringify({ cells: S.cells });
        body.values = errorValues();
      } else {
        body.content = area().value;
      }
      const res = await call('/api/studio/review', body);
      mergeNotes(res.notes || [], focus);
      el('studioReadState').textContent = res.spoke ? 'Said it on the speaker' : 'Sage read it';
    } catch (e) {
      el('studioReadState').textContent = 'Review failed: ' + e.message;
    } finally {
      S.reviewing = false;
    }
  }

  function mergeNotes(incoming, focus) {
    const lines = isText() ? textLines() : [];
    if (isText()) {
      S.notes = S.notes.filter(n => !n.global && !n.cell && !(n.line >= focus.from && n.line <= focus.to));
    } else {
      const changed = new Set(focus.cells || []);
      S.notes = S.notes.filter(n => !(n.cell && (changed.has(n.cell) || n.source === 'check')));
    }
    for (const n of incoming) { if (n.line) n.snap = lines[n.line - 1] ?? ''; S.notes.push(n); }
    if (isText()) renderText(); else renderSheet();
    renderNotes();
  }

  function renderNotes() {
    const notes = visibleNotes().slice().sort((a, b) => RANK[b.severity] - RANK[a.severity] || (a.line || 0) - (b.line || 0));
    const list = el('studioNoteList');
    list.innerHTML = notes.length ? notes.map((n, i) => `<div class="note n-${n.severity}" data-i="${S.notes.indexOf(n)}"><div class="nh"><b>${esc(place(n))}</b><span>${n.severity}${n.source === 'ai' ? ' · Sage' : n.source === 'tex' ? ' · LaTeX' : ''}</span></div><p>${esc(n.message)}</p>${n.replacement ? `<code>${esc(n.replacement)}</code>` : ''}<div class="na">${n.replacement ? '<button data-act="apply">Apply</button>' : ''}${n.line || n.cell ? '<button data-act="go">Go to</button>' : ''}<button data-act="dismiss">Dismiss</button></div></div>`).join('')
      : `<p class="meta">${S.doc ? `Nothing to point out. Sage checks again after every ${every()} new lines${S.doc.kind === 'sheet' ? ' (rows) you change' : ''}.` : 'Create or open a document to begin.'}</p>`;
    el('studioCount').textContent = notes.length ? String(notes.length) : '';
  }

  function onNoteClick(e) {
    const button = e.target.closest('button[data-act]');
    if (!button) return;
    const n = S.notes[parseInt(button.closest('.note').dataset.i, 10)];
    if (!n) return;
    if (button.dataset.act === 'dismiss') {
      S.dismissed.add(`${n.line ?? n.cell ?? ''}:${n.message}`);
    } else if (button.dataset.act === 'apply' && n.replacement !== undefined) {
      if (n.line) {
        const lines = textLines();
        lines[n.line - 1] = n.replacement;
        area().value = lines.join('\n');
        S.notes = S.notes.filter(x => x !== n);
        markDirty();
      } else if (n.cell) {
        commitCell(n.cell, n.replacement);
        S.notes = S.notes.filter(x => x !== n);
      }
    } else if (button.dataset.act === 'go') {
      if (n.line) goToLine(n.line); else if (n.cell) { select(n.cell); el('sheetScroll').focus(); }
    }
    if (isText()) renderText(); else if (S.doc && S.doc.kind === 'sheet') renderSheet();
    renderNotes();
  }

  function goToLine(line) {
    const a = area();
    const lines = textLines();
    let start = 0;
    for (let i = 0; i < line - 1; i++) start += lines[i].length + 1;
    a.focus();
    a.setSelectionRange(start, start + (lines[line - 1] || '').length);
    const lineHeight = parseFloat(getComputedStyle(a).lineHeight) || 20;
    a.scrollTop = Math.max(0, (line - 4) * lineHeight);
    syncScroll();
  }

  // ------------------------------------------------------------------ LaTeX compile and document preview

  function hidePreview() { el('studioPreview').classList.add('hidden'); el('studioPreview').dataset.mode = ''; }

  async function compile() {
    if (!S.doc || S.doc.kind !== 'latex') return;
    const button = el('studioCompile');
    button.disabled = true; button.textContent = 'Compiling…';
    try {
      const res = await call('/api/studio/compile', { content: area().value });
      S.notes = S.notes.filter(n => n.source !== 'tex');
      const lines = textLines();
      for (const n of res.notes || []) { if (n.line) n.snap = lines[n.line - 1] ?? ''; S.notes.push(n); }
      if (res.error) S.notes.push({ severity: 'error', message: res.error, source: 'tex', global: true });
      if (res.pdf_b64) {
        const bytes = Uint8Array.from(atob(res.pdf_b64), c => c.charCodeAt(0));
        if (S.pdfUrl) URL.revokeObjectURL(S.pdfUrl);
        S.pdfUrl = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }));
        const box = el('studioPreview');
        box.dataset.mode = 'pdf';
        box.innerHTML = `<iframe title="PDF preview" src="${S.pdfUrl}"></iframe>`;
        box.classList.remove('hidden');
      }
      say(res.ok ? 'Compiled' : 'Compile finished with errors');
      renderText(); renderNotes();
    } catch (e) { say(e.message); } finally { button.disabled = false; button.textContent = 'Compile PDF'; }
  }

  function markdown(text) {
    const inline = s => esc(s).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\*([^*\n]+)\*/g, '<i>$1</i>').replace(/`([^`]+)`/g, '<code>$1</code>');
    const out = []; let list = null, code = false;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of text.split('\n')) {
      if (line.trim().startsWith('```')) { close(); out.push(code ? '</pre>' : '<pre>'); code = !code; continue; }
      if (code) { out.push(esc(line) + '\n'); continue; }
      let m;
      if ((m = /^(#{1,6})\s+(.*)/.exec(line))) { close(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
      else if ((m = /^\s*[-*+]\s+(.*)/.exec(line))) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = /^\s*\d+[.)]\s+(.*)/.exec(line))) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
      else if (!line.trim()) close();
      else { close(); out.push(`<p>${inline(line)}</p>`); }
    }
    close(); if (code) out.push('</pre>');
    return out.join('');
  }

  function renderDocPreview() { el('studioPreview').innerHTML = `<div class="docPreview">${markdown(area().value)}</div>`; }

  function togglePreview() {
    const box = el('studioPreview');
    if (box.dataset.mode === 'doc') { hidePreview(); return; }
    box.dataset.mode = 'doc'; box.classList.remove('hidden'); renderDocPreview();
  }

  // ------------------------------------------------------------------ sheet editor

  function recompute() { S.values = SageSheet.compute(S.cells); }

  function errorValues() {
    const out = {};
    for (const [ref, v] of Object.entries(SageSheet.plain(S.values))) if (typeof v === 'string' && v[0] === '#') out[ref] = v;
    return out;
  }

  function renderSheet() {
    const marks = {};
    for (const n of visibleNotes()) if (n.cell && (!marks[n.cell] || RANK[n.severity] > RANK[marks[n.cell]])) marks[n.cell] = n.severity;
    let html = '<tr><th></th>' + Array.from({ length: COLUMNS }, (_, i) => `<th>${SageSheet.colLetters(i + 1)}</th>`).join('') + '</tr>';
    for (let r = 1; r <= S.rows; r++) {
      html += `<tr><th>${r}</th>`;
      for (let c = 1; c <= COLUMNS; c++) {
        const ref = SageSheet.colLetters(c) + r, v = S.values[ref];
        const isNumber = typeof v === 'number';
        const cls = (ref === S.sel ? 'sel ' : '') + (marks[ref] ? 'n-' + marks[ref] + ' ' : '') + (isNumber ? 'num ' : '') + (v instanceof SageSheet.Err ? 'err' : '');
        html += `<td data-ref="${ref}" class="${cls}">${esc(SageSheet.display(v))}</td>`;
      }
      html += '</tr>';
    }
    el('sheetGrid').innerHTML = html;
    el('sheetRef').textContent = S.sel;
    if (document.activeElement !== el('sheetInput')) el('sheetInput').value = S.cells[S.sel] ?? '';
  }

  function select(ref) {
    S.sel = ref;
    renderSheet();
    const cell = el('sheetGrid').querySelector(`td[data-ref="${ref}"]`);
    if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function move(dRow, dCol) {
    const m = /^([A-Z]+)(\d+)$/.exec(S.sel);
    const col = Math.min(COLUMNS, Math.max(1, SageSheet.colNumber(m[1]) + dCol));
    const row = Math.max(1, Math.min(S.rows, parseInt(m[2], 10) + dRow));
    select(SageSheet.colLetters(col) + row);
  }

  function commitCell(ref, raw) {
    raw = String(raw);
    if ((S.cells[ref] ?? '') === raw) return;
    if (raw === '') delete S.cells[ref]; else S.cells[ref] = raw;
    recompute();
    S.changed.add(ref);
    markDirty();
    const rows = new Set([...S.changed].map(r => r.replace(/^[A-Z]+/, '')));
    renderSheet();
    renderNotes();
    if (rows.size >= every()) {
      const cells = [...S.changed]; S.changed = new Set();
      clearTimeout(S.reviewTimer);
      S.reviewTimer = setTimeout(() => review({ cells }), 1200);
    }
  }

  function onSheetKey(e) {
    if (e.target === el('sheetInput')) return;
    const keys = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], Tab: [0, 1], Enter: [1, 0] };
    if (keys[e.key]) { e.preventDefault(); move(...keys[e.key]); return; }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); commitCell(S.sel, ''); return; }
    if (e.key === 'F2') { e.preventDefault(); el('sheetInput').focus(); return; }
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); const bar = el('sheetInput'); bar.value = e.key; bar.focus(); }
  }

  function onBarKey(e) {
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); commitCell(S.sel, e.target.value); move(e.key === 'Enter' ? 1 : 0, e.key === 'Tab' ? 1 : 0); el('sheetScroll').focus(); }
    else if (e.key === 'Escape') { e.target.value = S.cells[S.sel] ?? ''; el('sheetScroll').focus(); }
  }

  // ------------------------------------------------------------------ files

  function download(name, mime, b64) {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  async function doExport(format) {
    if (!S.doc || !format) return;
    if (S.dirty) await save();
    const content = S.doc.kind === 'sheet' ? JSON.stringify({ cells: S.cells }) : area().value;
    try {
      const res = await call('/api/studio/export', { kind: S.doc.kind, title: el('studioTitle').value, content, format });
      download(res.filename, res.mime, res.data_b64);
    } catch (e) { say(e.message); }
  }

  function toBase64(buffer) {
    let binary = '';
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }

  async function doImport(file) {
    if (!file) return;
    if (file.size > 5_000_000) { say('That file is larger than 5 MB'); return; }
    try {
      const res = await call('/api/studio/import', { filename: file.name, data_b64: toBase64(await file.arrayBuffer()) });
      await createDoc(res.kind, res.title, res.content);
      say(`Opened ${file.name}`);
    } catch (e) { say(e.message); }
  }

  // ------------------------------------------------------------------ setup

  function bind() {
    area().addEventListener('input', onTextInput);
    area().addEventListener('scroll', syncScroll);
    el('studioDocs').onchange = e => openDoc(e.target.value ? parseInt(e.target.value, 10) : null);
    el('studioNew').onclick = () => createDoc(el('studioNewKind').value, '').catch(e => say(e.message));
    el('studioEmptyNew').onclick = el('studioNew').onclick;
    el('studioTitle').oninput = markDirty;
    el('studioSave').onclick = () => save().catch(e => say(e.message));
    el('studioDelete').onclick = async () => {
      if (!S.doc || !confirm(`Delete "${S.doc.title}"? This cannot be undone.`)) return;
      await call(`/api/studio/doc/${S.doc.id}/delete`, {});
      S.doc = null; S.dirty = false;
      await loadDocs();
      const first = el('studioDocs').value;
      await openDoc(first ? parseInt(first, 10) : null);
    };
    el('studioReview').onclick = () => {
      if (!S.doc) return;
      if (S.doc.kind === 'sheet') review({ cells: Object.keys(S.cells).slice(0, 60) });
      else review({ from: 1, to: textLines().length });
    };
    el('studioCompile').onclick = compile;
    el('studioPreviewBtn').onclick = togglePreview;
    el('studioImport').onclick = () => el('studioFile').click();
    el('studioFile').onchange = e => { doImport(e.target.files[0]); e.target.value = ''; };
    el('studioExport').onchange = e => { doExport(e.target.value); e.target.value = ''; };
    el('studioNoteList').onclick = onNoteClick;
    el('studioEvery').onchange = () => { try { localStorage.setItem('studioEvery', String(every())); } catch { /* ignore */ } renderNotes(); };
    el('studioSpeak').onchange = () => { try { localStorage.setItem('studioSpeak', el('studioSpeak').value); } catch { /* ignore */ } };
    el('sheetGrid').onclick = e => { const td = e.target.closest('td[data-ref]'); if (td) { select(td.dataset.ref); el('sheetScroll').focus(); } };
    el('sheetGrid').ondblclick = e => { if (e.target.closest('td[data-ref]')) el('sheetInput').focus(); };
    el('sheetScroll').addEventListener('keydown', onSheetKey);
    el('sheetInput').addEventListener('keydown', onBarKey);
    el('sheetInput').addEventListener('blur', e => { if (S.doc && S.doc.kind === 'sheet') commitCell(S.sel, e.target.value); });
    el('sheetMore').onclick = () => { S.rows = Math.min(500, S.rows + 20); renderSheet(); };
    window.addEventListener('beforeunload', () => { if (S.dirty && S.doc) navigator.sendBeacon && navigator.sendBeacon(`/api/studio/doc/${S.doc.id}`, new Blob([JSON.stringify({ title: el('studioTitle').value, content: S.doc.kind === 'sheet' ? JSON.stringify({ cells: S.cells }) : area().value })], { type: 'application/json' })); });
  }

  async function init() {
    if (S.inited) return;
    S.inited = true;
    bind();
    try { el('studioEvery').value = localStorage.getItem('studioEvery') || '5'; el('studioSpeak').value = localStorage.getItem('studioSpeak') || 'errors'; } catch { /* ignore */ }
    try {
      const status = await call('/api/studio/status');
      S.engine = status.latex_engine; S.room = status.speaker_room;
      el('studioHint').textContent = `Sage reads along. After ${every()} new lines it checks them and leaves notes here. The speaker only speaks while your beacon is in the ${S.room}.`;
      if (!S.engine) el('studioCompile').title = 'No LaTeX engine is installed on the Sage machine yet';
    } catch { /* status is optional */ }
    await loadDocs();
    let last = null;
    try { last = parseInt(localStorage.getItem('studioLast'), 10); } catch { /* ignore */ }
    const target = S.docs.find(d => d.id === last) || S.docs[0];
    await openDoc(target ? target.id : null);
  }

  window.SageStudio = { init, state: S, markdown, review, renderNotes };
  const startup = () => {
    const button = document.querySelector('nav button[data-page="studio"]');
    if (button) button.addEventListener('click', () => init().catch(e => say(e.message)));
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startup); else startup();
})();
