(() => {
    const p = document.querySelector('#plannerMini');
    const c = document.querySelector('#councilMini');
    const esc = s => String(s ?? '').replace(/[&<>"']/g, x => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[x]));

    async function load() {
        try {
            const [plan, status] = await Promise.all([
                fetch('/api/plan', { cache: 'no-store' }).then(r => r.json()),
                fetch('/api/status', { cache: 'no-store' }).then(r => r.json())
            ]);
            const active = [
                ...(plan.running || []).map(x => ({ ...x, _state: 'running' })),
                ...(plan.queue || []).slice(0, 4).map(x => ({ ...x, _state: 'pending' })),
                ...(plan.needs_you || []).slice(0, 2).map(x => ({ ...x, _state: 'pending' }))
            ];
            p.innerHTML = active.length
                ? active.slice(0, 6).map(x => '<div class="drawerTask"><span class="drawerDot ' + (x._state === 'running' ? 'running' : '') + '"></span><span>' + esc(x.title || 'Untitled task') + '<small>' + esc(x.project || x.assignee || x.status || x._state) + '</small></span></div>').join('')
                : '<div class="drawerTask"><span class="drawerDot done"></span><span>No active plan items<small>Planner is clear</small></span></div>';

            const modelStatus = status.ollama || {};
            const isLlamaCpp = modelStatus.backend === 'llama.cpp';
            const backendName = isLlamaCpp ? 'llama.cpp' : 'Ollama';
            const online = !!modelStatus.online && (isLlamaCpp || !!modelStatus.installed);
            c.innerHTML = '<div class="drawerTask"><span class="drawerDot ' + (online ? 'done' : '') + '"></span><span>' + esc(status.model || 'Local model') + '<small>' + backendName + (online ? ' ready' : ' unavailable') + '</small></span></div><p class="meta">Council verdicts are not available in this Sage configuration.</p>';

            const lanes = modelStatus.lanes;
            if (Array.isArray(lanes) && lanes.length) {
                const speeds = status.speed?.by_model || {};
                c.innerHTML = lanes.map(l => {
                    const detail = l.installed
                        ? (speeds[l.model] ? speeds[l.model].tokens_per_second + ' tok/s avg' : 'ready')
                        : (isLlamaCpp ? 'server unavailable' : 'not installed · run: ollama pull ' + l.model);
                    return '<div class="drawerTask"><span class="drawerDot ' + (l.installed ? 'done' : '') + '"></span><span>' + esc(l.model) + '<small>' + esc(l.role) + ' lane · ' + esc(detail) + '</small></span></div>';
                }).join('');
            }

            const chip = document.querySelector('#modelChip');
            if (chip) chip.textContent = (isLlamaCpp ? 'LLAMA.CPP' : 'OLLAMA') + ' · ' + (online ? 'READY' : 'OFFLINE');
        } catch {
            p.textContent = 'Planner status unavailable';
            c.textContent = 'Model status unavailable';
        }
    }

    load();
    setInterval(load, 15000);
})();
