/* Planner page + live notifications (reminders, timers, Sage's work order, schedules, memory).
   Loaded after app.js and uses its helpers: $, esc, api, toast, pages, loadPage. */
(function () {
  pages.planner = ['Planner', "Sage's work order, reminders, schedules and memory"];
  const css = document.createElement('style');
  css.textContent = `
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}.tabs button{border:1px solid var(--line,#2a3550);background:transparent;color:inherit;padding:8px 14px;border-radius:999px;cursor:pointer}
  .tabs button.on{background:var(--accent,#4f8cff);border-color:var(--accent,#4f8cff);color:#fff}
  .qrow{display:flex;align-items:center;gap:12px;padding:10px 14px;margin:8px 0;cursor:grab}.qrow.dragging{opacity:.4}.qrow.over{outline:2px dashed var(--accent,#4f8cff)}
  .qrow .grip{opacity:.4;font-size:18px}.qrow .pos{min-width:1.6em;text-align:center;font-size:18px}.qrow .qmain{flex:1;min-width:0}.qrow .qact{display:flex;gap:6px;flex-wrap:wrap}
  .qrow .qact button,.pBtn{border:1px solid var(--line,#2a3550);background:transparent;color:inherit;border-radius:8px;padding:4px 9px;cursor:pointer;font-size:13px}
  .badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:rgba(120,140,190,.2);margin-right:6px}
  .badge.sage{background:rgba(52,208,186,.2)}.badge.user{background:rgba(255,188,76,.25)}
  .plannerBar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.plannerBar input[type=text]{max-width:220px}
  #ringBanner{position:fixed;left:0;right:0;top:0;z-index:50;display:none;padding:10px 16px;background:#b3382c;color:#fff;box-shadow:0 4px 18px rgba(0,0,0,.4)}
  #ringBanner .ring{display:flex;gap:12px;align-items:center;margin:4px 0}#ringBanner .ring b{flex:1}#ringBanner button{border:0;border-radius:8px;padding:6px 12px;cursor:pointer}
  .subhead2{margin:22px 0 6px;font-size:15px;opacity:.8}.pill{opacity:.7;font-size:12px}
  .needBox{background:rgba(255,188,76,.12);border-left:4px solid #ffbc4c;padding:8px 14px;border-radius:8px;margin:12px 0}.needBox p{margin:6px 0;line-height:1.5}
  .qrow.open{cursor:pointer}.qrow.open:hover{outline:1px solid var(--accent,#4f8cff)}.qrow .ask{opacity:.85;font-size:13px;margin-top:2px}`;
  document.head.append(css);
  document.querySelector('#nav').insertAdjacentHTML('afterbegin', '<button data-page="planner">▶ <span>Planner</span></button>');
  document.querySelector('main').insertAdjacentHTML('beforeend', '<section id="page-planner" class="page"><div class="sectionHead"><div><h2>Planner</h2><p>The order Sage works in, your reminders and timers, schedules, and what it remembers.</p></div></div><div class="tabs" id="plTabs"></div><div id="plRoot"></div></section><div id="ringBanner"></div>');

  const S = { tab: 'plan', mem: 'suggested', task: null };
  const TABS = [['plan', 'Work plan'], ['rem', 'Reminders & timers'], ['sch', 'Schedules'], ['mem', 'Memory']];
  const call = (url, body) => api(url, body).catch(e => { toast(e.message); throw e; });
  const mins = m => m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60 ? m % 60 + ' min' : ''}`;
  const when = iso => { try { return new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' }) } catch { return iso } };

  function tabs() { $('#plTabs').innerHTML = TABS.map(([k, n]) => `<button data-tab="${k}" class="${S.tab === k ? 'on' : ''}">${n}</button>`).join(''); }

  // ------------------------------------------------------------- work plan
  function taskRow(i, n) {
    const eta = i.eta_start_minutes === 0 ? 'next up' : `starts in ~${mins(i.eta_start_minutes)}`;
    return `<article class="qrow card" draggable="true" data-id="${i.id}"><span class="grip">⋮⋮</span><b class="pos">${i.position || ''}</b>
      <div class="qmain"><strong>${esc(i.title)}</strong><div class="meta"><span class="badge ${i.assignee}">${i.assignee === 'sage' ? 'Sage' : 'You'}</span><span class="badge">${esc(i.kind)}</span>${esc(i.project || 'no project')} · ~${mins(i.estimate_minutes)} · ${eta}${i.milestone ? ' · ' + esc(i.milestone) : ''}</div>
      ${i.waiting_on && i.waiting_on.length ? `<div class="meta">after: ${esc(i.waiting_titles.join(', '))}</div>` : ''}</div>
      <div class="qact"><button data-act="up" title="Move up">▲</button><button data-act="down" title="Move down">▼</button><button data-act="top" title="Make this first">⤒ First</button><button data-act="now">Do now</button><button data-act="me">I'll do it</button><button data-act="pause">Pause</button></div></article>`;
  }
  function needRow(i) {
    const ask = esc(String(i.question || '').replace(/\s+/g, ' ').slice(0, 160));
    return `<article class="qrow card open" data-id="${i.id}" data-open="1"><div class="qmain"><strong>${esc(i.title)}</strong><div class="meta"><span class="badge ${i.assignee}">${i.assignee === 'sage' ? 'Sage asks' : 'You do'}</span><span class="badge">${esc(i.kind)}</span>${esc(i.project || 'no project')}</div><div class="ask">${ask || 'Tap to see what is needed'}</div></div><div class="qact"><button data-act="open" class="primary">Open</button></div></article>`;
  }
  function miniRow(i, buttons) {
    return `<article class="qrow card" data-id="${i.id}" style="cursor:default"><div class="qmain"><strong>${esc(i.title)}</strong><div class="meta">${esc(i.project || 'no project')} · ${esc(i.kind)}${i.blocked_reason ? ' · ' + esc(i.blocked_reason) : ''}${i.waiting_titles && i.waiting_titles.length ? ' · waiting for: ' + esc(i.waiting_titles.join(', ')) : ''}</div></div><div class="qact">${buttons}</div></article>`;
  }
  async function loadPlan() {
    const v = await call('/api/plan');
    const blockedCount = v.needs_you.filter(i => i.status === 'blocked').length;
    let h = `<div class="card plannerBar"><label><input type="checkbox" id="apToggle" ${v.autopilot ? 'checked' : ''}> Autopilot</label><span class="meta">Work only between</span><input type="text" id="apWindow" value="${esc(v.window)}" placeholder="any time, or 22:00-07:00"><button class="softBtn" id="apSave">Save</button><span class="meta">${v.queue.length} queued · about ${mins(v.total_eta_minutes)} of work${v.autopilot && !v.in_window ? ' · outside the work window right now' : ''}</span></div>
      ${blockedCount ? `<div class="card plannerBar"><span class="meta">${blockedCount} blocked task${blockedCount === 1 ? '' : 's'}. Use this only after reviewing and approving their completed work.</span><button class="softBtn" id="approveBlockedPlanner">Mark all blocked tasks done</button></div>` : ''}
      <form id="planForm" class="formCard"><textarea name="goal" placeholder="Describe a goal and Sage drafts a plan: milestones, ordered tasks, estimates and who does what…" required></textarea><button class="primary">Draft a plan</button></form>`;
    if (v.running.length) h += `<h3 class="subhead2">Working on now</h3>` + v.running.map(i => `<article class="card"><strong>${esc(i.title)}</strong><div class="meta">${esc(i.project)} · started</div></article>`).join('');
    h += `<h3 class="subhead2">Sage's queue — drag to reorder, or use the buttons</h3>`;
    h += v.queue.length ? v.queue.map(taskRow).join('') : '<p class="meta">The queue is empty. Ask Sage for a plan, or add tasks to a project.</p>';
    h += `<h3 class="subhead2">Needs you — click a task to see what it needs</h3>` + (v.needs_you.length ? v.needs_you.map(needRow).join('') : '<p class="meta">Nothing is waiting for you. When Sage gets stuck and has a question, or a task is yours (soldering, buying, deciding), it shows up here. Add one below to try it.</p>');
    h += `<details class="card"><summary>Add a task</summary><form id="addTaskForm" class="formCard"><input name="title" placeholder="Task title, e.g. Order the depth sensor" required><textarea name="description" placeholder="Details (optional)"></textarea><select name="assignee"><option value="user">I will do it (shows up under Needs you)</option><option value="sage">Sage does it</option></select><button class="primary">Add task</button></form></details>`;
    if (v.waiting.length) h += `<h3 class="subhead2">Waiting on something else</h3>` + v.waiting.map(i => miniRow(i, `<button data-act="now">Do now</button>`)).join('');
    if (v.paused.length) h += `<h3 class="subhead2">Paused</h3>` + v.paused.map(i => miniRow(i, `<button data-act="resume">Resume</button>`)).join('');
    if (v.recent_done.length) h += `<h3 class="subhead2">Recently finished</h3>` + v.recent_done.map(i => `<article class="card"><strong>${esc(i.title)}</strong><div class="meta">${esc(i.project)} · ${esc(i.summary || '')}</div></article>`).join('');
    $('#plRoot').innerHTML = h;
    const approveButton = $('#approveBlockedPlanner');
    if (approveButton) approveButton.onclick = async () => {
      if (!confirm(`Confirm that you have reviewed and approve the completed work for all ${blockedCount} blocked task${blockedCount === 1 ? '' : 's'}? They will be marked done and removed from the active Planner.`)) return;
      try {
        const result = await call('/api/plan/approve-blocked', { confirmed: true });
        toast(`${result.updated} blocked task${result.updated === 1 ? '' : 's'} marked done`);
        await loadPlan();
      } catch { /* error already shown */ }
    };
  }
  async function planAction(id, act, row) {
    if (act === 'open') return showTask(id);
    const pos = row && row.querySelector('.pos') ? parseInt(row.querySelector('.pos').textContent, 10) || 1 : 1;
    if (act === 'up') await call('/api/plan/move', { task_id: id, position: Math.max(1, pos - 1) });
    else if (act === 'down') await call('/api/plan/move', { task_id: id, position: pos + 1 });
    else if (act === 'top') await call('/api/plan/move', { task_id: id, position: 1 });
    else if (act === 'now') { await call('/api/plan/run-now', { task_id: id }); toast('Queued. Sage starts as soon as you stop chatting.'); }
    else if (act === 'me' || act === 'give') await call('/api/plan/task/' + id, { assignee: act === 'me' ? 'user' : 'sage' });
    else if (act === 'pause' || act === 'resume') await call('/api/plan/task/' + id, { paused: act === 'pause' });
    else if (act === 'done') await call('/api/plan/task/' + id, { status: 'done' });
    else if (act === 'retry') await call('/api/plan/task/' + id, { status: 'todo' });
    loadPlan();
  }

  // ------------------------------------------------------------- reminders
  async function loadRem() {
    const r = await call('/api/reminders');
    let h = `<form id="remForm" class="formCard"><input name="title" placeholder="Remind me to…" required><input name="when" placeholder="when: tomorrow 18:00, in 20 minutes, every monday 9" required><button class="primary">Add reminder</button></form>
      <div class="row" style="margin:8px 0">${[5, 10, 25, 45].map(m => `<button class="softBtn" data-timer="${m}">⏱ ${m} min timer</button>`).join('')}</div>`;
    if (r.alerts.length) h += '<h3 class="subhead2">Ringing now</h3>' + r.alerts.map(a => `<article class="card"><strong>${esc(a.title)}</strong> <span class="pill">${esc(a.kind)}</span><div class="row"><button class="primary" data-ra="done" data-id="${a.id}">Done</button><button class="softBtn" data-ra="snooze" data-id="${a.id}">Snooze 10 min</button></div></article>`).join('');
    h += '<h3 class="subhead2">Coming up</h3>' + (r.active.length ? r.active.map(a => `<article class="card"><strong>${esc(a.title)}</strong> <span class="pill">${esc(a.kind)}</span><div class="meta" data-due="${esc(a.due_at)}" data-kind="${esc(a.kind)}">${esc(a.when)}${a.repeat ? ' · ' + esc(a.repeat) : ''}</div><div class="row"><button class="softBtn" data-ra="cancel" data-id="${a.id}">Cancel</button></div></article>`).join('') : '<p class="meta">Nothing scheduled. Try “set a timer for 10 minutes” in chat.</p>');
    $('#plRoot').innerHTML = h;
  }

  // ------------------------------------------------------------- schedules
  async function loadSch() {
    const d = await call('/api/schedules');
    let h = `<form id="schForm" class="formCard"><input name="name" placeholder="Name, e.g. Monday ROV summary" required><input name="when" placeholder="when: every monday at 9, every weekday at 7:30, every 2 hours" required><textarea name="prompt" placeholder="What should Sage do? e.g. Summarise the ROV project and what is blocked." required></textarea><button class="primary">Add schedule</button></form>`;
    h += d.schedules.map(s => `<article class="card"><strong>${esc(s.name)}</strong> <span class="pill">${s.builtin ? 'built in' : 'yours'}</span><div class="meta">${esc(s.when)} · ${esc(s.description || (s.payload && s.payload.prompt) || '')}</div><div class="meta">Last: ${esc(s.last_status || 'never run')}${s.next_run ? ' · next: ' + esc(when(s.next_run)) : ''}</div><div class="row"><button class="softBtn" data-sa="${s.enabled ? 'disable' : 'enable'}" data-id="${s.id}">${s.enabled ? 'Turn off' : 'Turn on'}</button><button class="softBtn" data-sa="run" data-id="${s.id}">Run now</button>${s.builtin ? '' : `<button class="softBtn" data-sa="delete" data-id="${s.id}">Delete</button>`}</div></article>`).join('');
    $('#plRoot').innerHTML = h;
  }

  // ------------------------------------------------------------- memory
  async function loadMem() {
    const list = await call('/api/memories?status=' + S.mem);
    const sel = ['suggested', 'active', 'archived'].map(k => `<button data-ms="${k}" class="${S.mem === k ? 'on' : ''}">${k[0].toUpperCase() + k.slice(1)}</button>`).join('');
    let h = `<div class="tabs">${sel}</div>` + (S.mem === 'suggested' ? '<p class="meta">Things Sage thinks it should remember, but is not sure about. Accept the ones that are right.</p>' : '');
    h += list.length ? list.map(m => `<article class="card"><div>${m.pinned ? '📌 ' : ''}${esc(m.content)}</div><div class="meta">${esc(m.kind)} · importance ${m.importance}/5 · used ${m.use_count}×${m.confidence ? ' · confidence ' + Math.round(m.confidence * 100) + '%' : ''}</div><div class="row">${S.mem === 'suggested' ? `<button class="primary" data-ma="accept" data-id="${m.id}">Remember</button><button class="softBtn" data-ma="archive" data-id="${m.id}">Dismiss</button>` : S.mem === 'archived' ? `<button class="softBtn" data-ma="accept" data-id="${m.id}">Restore</button>` : `<button class="softBtn" data-ma="${m.pinned ? 'unpin' : 'pin'}" data-id="${m.id}">${m.pinned ? 'Unpin' : 'Pin'}</button><button class="softBtn" data-ma="more" data-id="${m.id}" data-i="${m.importance}">Importance +</button><button class="softBtn" data-ma="less" data-id="${m.id}" data-i="${m.importance}">−</button><button class="softBtn" data-ma="archive" data-id="${m.id}">Forget</button>`}</div></article>`).join('') : '<p class="meta">Nothing here.</p>';
    $('#plRoot').innerHTML = h;
  }

  async function showTask(id) {
    S.task = id;
    let d;
    try { d = await call('/api/plan/task/' + id); } catch { S.task = null; return loadPlan(); }
    const mine = d.kind === 'physical' || d.assignee !== 'sage';
    const lines = t => esc(t).replace(/\n/g, '<br>');
    $('#plRoot').innerHTML = `<button class="softBtn" data-back="1">← Back to the plan</button>
      <article class="card"><h3>${esc(d.title)}</h3><div class="meta"><span class="badge ${d.assignee}">${d.assignee === 'sage' ? 'Sage' : 'You'}</span><span class="badge">${esc(d.kind)}</span>${esc(d.project || 'no project')}${d.milestone ? ' · ' + esc(d.milestone) : ''} · ${esc(d.status)}</div>
      <div class="needBox"><b>${mine ? 'What you need to do' : 'What Sage needs from you'}</b><p>${lines(d.question || 'Sage did not say exactly. Tell it what to do next.')}</p></div>
      ${d.acceptance ? `<div class="meta"><b>Done when:</b> ${esc(d.acceptance)}</div>` : ''}${d.depends_on.length ? `<div class="meta"><b>After:</b> ${esc(d.depends_on.join(', '))}</div>` : ''}
      ${d.result_summary ? `<div class="meta"><b>Sage's last result:</b> ${esc(d.result_summary)}</div>` : ''}
      ${d.deliverable ? `<details><summary>Show Sage's work so far</summary><pre class="engineeringBody">${esc(d.deliverable)}</pre></details>` : ''}
      ${d.previous_input ? `<details><summary>Your earlier answers</summary><pre class="engineeringBody">${esc(d.previous_input)}</pre></details>` : ''}</article>
      <form id="taskInputForm" class="formCard" data-id="${d.id}" data-mine="${mine ? 1 : 0}"><textarea name="text" ${mine ? '' : 'required'} placeholder="${mine ? 'What happened? Notes, measurements or decisions (optional)' : 'Your answer, or the information Sage asked for…'}"></textarea>
      <div class="row"><button class="primary">${mine ? 'I did it: mark done' : 'Send to Sage and continue'}</button>${mine ? '<button type="button" class="softBtn" data-tk="sage">Let Sage do it instead</button>' : (d.status === 'blocked' ? '<button type="button" class="softBtn" data-tk="retry">Try again as is</button>' : '') + '<button type="button" class="softBtn" data-tk="self">I will handle it myself: mark done</button>'}<button type="button" class="softBtn" data-tk="delete">Delete task</button></div></form>`;
  }
  function leaveTask() { S.task = null; }

  function show() { tabs(); if (S.tab === 'plan' && S.task) { showTask(S.task); return; } ({ plan: loadPlan, rem: loadRem, sch: loadSch, mem: loadMem })[S.tab]().catch(err => { $('#plRoot').innerHTML = `<div class="card"><b>The Planner could not load.</b><p>${esc((err && err.message) || 'unknown error')}</p><p class="meta">If you just updated Sage, restart it (close its black window and run start-sage.bat again), then press Ctrl+F5 in this tab.</p></div>`; }); }
  document.querySelector('#nav').addEventListener('click', e => { const b = e.target.closest('button[data-page]'); if (b && b.dataset.page === 'planner') show(); });

  // ------------------------------------------------------------- clicks and forms
  document.addEventListener('click', e => {
    const r = e.target.closest('[data-open]'); if (r && !e.target.closest('button')) showTask(+r.dataset.id);
  });
  document.addEventListener('click', async e => {
    const t = e.target.closest('button,[data-tab]'); if (!t || t.closest('#ringBanner')) return;
    if (t.dataset.tab) { S.tab = t.dataset.tab; leaveTask(); return show(); }
    if (t.dataset.back) { leaveTask(); return loadPlan(); }
    if (t.dataset.tk) {
      const id = +t.closest('form').dataset.id;
      try {
        if (t.dataset.tk === 'sage') await call('/api/plan/task/' + id, { assignee: 'sage', kind: 'doc', status: 'todo' });
        else if (t.dataset.tk === 'retry') await call(`/api/plan/task/${id}/input`, { retry: true });
        else if (t.dataset.tk === 'delete') { if (!confirm('Delete this task for good?')) return; await call(`/api/plan/task/${id}/delete`, {}); }
        else await call(`/api/plan/task/${id}/input`, { text: t.closest('form').text.value, mark_done: true });
      } catch { return; }
      toast({ sage: 'Sage will take this task', retry: 'Sage will try again', delete: 'Task deleted', self: 'Marked done' }[t.dataset.tk]); leaveTask(); return loadPlan();
    }
    if (t.dataset.ms) { S.mem = t.dataset.ms; return loadMem(); }
    const row = t.closest('[data-id]'), id = t.dataset.id ? +t.dataset.id : row && +row.dataset.id;
    try {
      if (t.id === 'apSave') { await call('/api/plan/autopilot', { enabled: $('#apToggle').checked, window: $('#apWindow').value }); toast('Saved'); return loadPlan(); }
      if (t.dataset.act) return planAction(id, t.dataset.act, row);
      if (t.dataset.timer) { await call('/api/reminders', { kind: 'timer', duration: t.dataset.timer + ' minutes', title: t.dataset.timer + ' minute timer' }); toast('Timer started'); return loadRem(); }
      if (t.dataset.ra) { await call(`/api/reminders/${id}/${t.dataset.ra}`, t.dataset.ra === 'snooze' ? { minutes: 10 } : {}); return loadRem(); }
      if (t.dataset.sa) { const a = t.dataset.sa; if (a === 'delete') await call(`/api/schedules/${id}/delete`, {}); else if (a === 'run') { await call(`/api/schedules/${id}/run`, {}); toast('Started'); } else await call('/api/schedules/' + id, { enabled: a === 'enable' }); return loadSch(); }
      if (t.dataset.ma) {
        const a = t.dataset.ma, body = { accept: { status: 'active' }, archive: { status: 'archived' }, pin: { pinned: true, importance: 5 }, unpin: { pinned: false },
          more: { importance: Math.min(5, +t.dataset.i + 1) }, less: { importance: Math.max(1, +t.dataset.i - 1) } }[a];
        await call('/api/memories/' + id, body); return loadMem();
      }
    } catch { /* toast already shown */ }
  });
  document.addEventListener('submit', async e => {
    const f = e.target, d = Object.fromEntries(new FormData(f));
    try {
      if (f.id === 'planForm') { e.preventDefault(); await call('/api/plan/generate', { goal: d.goal }); f.reset(); toast('Drafting the plan in the background. You will get a notification.'); }
      else if (f.id === 'addTaskForm') { e.preventDefault(); await call('/api/plan/task', d); toast('Task added'); loadPlan(); }
      else if (f.id === 'taskInputForm') { e.preventDefault(); const r = await call(`/api/plan/task/${f.dataset.id}/input`, { text: d.text, mark_done: f.dataset.mine === '1' }); toast(r.status === 'done' ? 'Marked done. Nice.' : 'Sent. Sage continues as soon as you stop chatting.'); leaveTask(); loadPlan(); }
      else if (f.id === 'remForm') { e.preventDefault(); await call('/api/reminders', d); f.reset(); loadRem(); }
      else if (f.id === 'schForm') { e.preventDefault(); await call('/api/schedules', { name: d.name, when: d.when, prompt: d.prompt }); f.reset(); loadSch(); }
    } catch { /* shown */ }
  });
  // drag and drop reordering
  let dragged = null;
  document.addEventListener('dragstart', e => { const r = e.target.closest && e.target.closest('.qrow[draggable]'); if (!r) return; dragged = r; r.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
  document.addEventListener('dragover', e => { const r = e.target.closest && e.target.closest('.qrow[draggable]'); if (!r || !dragged) return; e.preventDefault(); document.querySelectorAll('.qrow.over').forEach(x => x.classList.remove('over')); if (r !== dragged) r.classList.add('over'); });
  document.addEventListener('dragend', () => { document.querySelectorAll('.qrow').forEach(x => x.classList.remove('dragging', 'over')); dragged = null; });
  document.addEventListener('drop', async e => {
    const target = e.target.closest && e.target.closest('.qrow[draggable]'); if (!target || !dragged || target === dragged) return; e.preventDefault();
    const rows = [...document.querySelectorAll('#plRoot .qrow[draggable]')], from = rows.indexOf(dragged), to = rows.indexOf(target);
    rows.splice(to, 0, rows.splice(from, 1)[0]);
    try { await call('/api/plan/reorder', { task_ids: rows.map(r => +r.dataset.id) }); } catch { /* shown */ }
    loadPlan();
  });

  // ------------------------------------------------------------- live events (SSE): toasts, ringing banner, beeps
  const ringing = new Map(); let beepTimer = null, audio = null;
  function beep() { try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); const o = audio.createOscillator(), g = audio.createGain(); o.frequency.value = 880; g.gain.value = 0.12; o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + 0.18); } catch { /* audio not allowed yet */ } }
  function renderBanner() {
    const b = $('#ringBanner');
    if (!ringing.size) { b.style.display = 'none'; clearInterval(beepTimer); beepTimer = null; return; }
    b.style.display = 'block';
    b.innerHTML = [...ringing.values()].map(a => `<div class="ring"><b>⏰ ${esc(a.title)}</b><button data-ra="done" data-id="${a.id}">Done</button><button data-ra="snooze" data-id="${a.id}">Snooze 10 min</button></div>`).join('');
    if (!beepTimer) { beep(); beepTimer = setInterval(beep, 1500); }
  }
  document.addEventListener('click', async e => {
    const t = e.target.closest('#ringBanner button'); if (!t) return;
    const id = +t.dataset.id; ringing.delete(id); renderBanner();
    try { await api(`/api/reminders/${id}/${t.dataset.ra}`, t.dataset.ra === 'snooze' ? { minutes: 10 } : {}); } catch (err) { toast(err.message); }
  });
  function onEvent(ev) {
    const k = ev.kind, d = ev.data || {};
    if (k === 'timer.done' || k === 'reminder.due') {
      ringing.set(d.id, { id: d.id, title: d.title || ev.title }); renderBanner();
      if (window.Notification && Notification.permission === 'granted') new Notification(ev.title, { body: ev.body || '' });
    } else if (['reminder.done', 'reminder.snoozed', 'reminder.cancelled'].includes(k)) { ringing.delete(d.id); renderBanner(); }
    else if (ev.level !== 'info' || ['reminder.created', 'timer.created'].includes(k)) toast(ev.title + (ev.body && ev.level !== 'alert' ? ': ' + ev.body.slice(0, 80) : ''));
    else if (k === 'memory.saved') toast('Remembered: ' + (ev.body || '').slice(0, 80));
    if (document.querySelector('#page-planner.active') && /^(plan|task|project|reminder|timer|schedule|memory|job)/.test(k) && !S.task) setTimeout(show, 150);
    if (k === 'brief.ready' && typeof loadChat === 'function' && document.querySelector('#page-chat.active') && !sageBusy) loadChat();
  }
  try {
    const es = new EventSource('/api/events/stream'); es.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch { /* ignore */ } };
    api('/api/reminders').then(r => { r.alerts.forEach(a => ringing.set(a.id, a)); renderBanner(); }).catch(() => {});
  } catch { /* EventSource unsupported */ }
})();
