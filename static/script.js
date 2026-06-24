/* ════════════════════════════════════════════════════════════════════
   Ad Floor Studio — frontend (vanilla, lightweight, animated)
   ════════════════════════════════════════════════════════════════════ */
const $  = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const S = {
    basic: [], detailed: [], site: null, trends: null,
    tab: 'basic', sortCol: null, sortDir: 'asc', q: ''
};

/* ── helpers ─────────────────────────────────────────────── */
const esc = s => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };
const n2  = v => (Number(v) || 0).toFixed(2);
const money = v => '$' + (Number(v) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
const intc  = v => (Number(v) || 0).toLocaleString();

// animated count-up for a KPI value element
function countUp(el, target, prefix = '', suffix = '', decimals = 0) {
    const dur = 850, t0 = performance.now();
    const ease = t => 1 - Math.pow(1 - t, 3);
    function step(now) {
        const p = Math.min(1, (now - t0) / dur);
        const v = target * ease(p);
        el.textContent = prefix + v.toLocaleString(undefined,
            { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix;
        if (p < 1) requestAnimationFrame(step);
        else el.textContent = prefix + target.toLocaleString(undefined,
            { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix;
    }
    requestAnimationFrame(step);
}

/* ── uploads ─────────────────────────────────────────────── */
function wireDrop(dropId, inputId, chipId, nameId, xId, onChange) {
    const drop = $('#' + dropId), input = $('#' + inputId),
          chip = $('#' + chipId), nm = $('#' + nameId), x = $('#' + xId);
    drop.addEventListener('click', () => input.click());
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
    drop.addEventListener('drop', e => {
        e.preventDefault(); drop.classList.remove('drag');
        if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; sel(); }
    });
    input.addEventListener('change', sel);
    x.addEventListener('click', e => {
        e.stopPropagation(); input.value = '';
        chip.style.display = 'none'; drop.classList.remove('filled'); onChange();
    });
    function sel() {
        if (!input.files.length) return;
        nm.textContent = input.files[0].name;
        chip.style.display = 'flex'; drop.classList.add('filled'); onChange();
    }
}
const fileMain = () => $('#file-main');
const fileBid  = () => $('#file-bid');
const refreshGo = () => { $('#btn-go').disabled = !fileMain().files.length; };
wireDrop('drop-main', 'file-main', 'chip-main', 'name-main', 'x-main', refreshGo);
wireDrop('drop-bid',  'file-bid',  'chip-bid',  'name-bid',  'x-bid',  refreshGo);

/* ── analyze ─────────────────────────────────────────────── */
$('#btn-go').addEventListener('click', async () => {
    if (!fileMain().files.length) return;
    toggle('#loader', true); toggle('#err', false); $('#results').classList.remove('on');
    const fd = new FormData();
    fd.append('file', fileMain().files[0]);
    if (fileBid().files.length) fd.append('bid_file', fileBid().files[0]);
    try {
        const r = await fetch('/api/analyze', { method: 'POST', body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Analysis failed.');
        S.basic = d.basic || []; S.detailed = d.detailed || [];
        S.site = d.site_performance || null; S.trends = d.day_of_week_trends || null;

        renderKpis(d);
        renderInsights(d.insights || []);
        $('#cnt-basic').textContent = S.basic.length;
        $('#cnt-detailed').textContent = S.detailed.length;
        $('#meta').textContent =
            `${intc(d.rows_processed)} rows · ${intc(d.total_segments_basic)} / ${intc(d.total_segments_detailed)} segments scanned`
            + ` · ${S.basic.length} / ${S.detailed.length} actionable`;

        $('#results').classList.add('on');
        switchTab('basic', true);
    } catch (e) { showErr(e.message); }
    finally { toggle('#loader', false); }
});

const toggle = (sel, on) => $(sel).classList.toggle('on', on);
function showErr(m) { $('#err-msg').textContent = m; toggle('#err', true); }
$('#err-x').addEventListener('click', () => toggle('#err', false));

/* ── KPI strip ───────────────────────────────────────────── */
function renderKpis(d) {
    const sp = d.site_performance || {}, up = d.realized_uplift || {}, hasReq = !!sp.has_requests;
    const cards = [
        { lbl: 'Network Revenue', val: sp.total_revenue || 0, pre: '$', dec: 0, sub: `${sp.analyzed_sites || 0} sites · ${intc(sp.total_imps)} imps` },
        { lbl: 'Avg eCPM', val: sp.avg_ecpm || 0, pre: '$', dec: 2, sub: 'cleared, network-wide' },
        { lbl: 'Avg RPM', val: hasReq ? (sp.avg_rpm || 0) : null, pre: '$', dec: 2, sub: hasReq ? 'revenue / request' : 'add bid landscape' },
        { lbl: 'Actionable Floors', val: S.basic.length, pre: '', dec: 0, sub: `${intc(d.total_segments_basic)} segments` },
    ];
    const applied = up.applied_segments || 0;
    if (applied > 0 && up.rev_uplift_pct != null) {
        cards.push({ lbl: 'Measured Uplift', valTxt: (up.rev_uplift_pct >= 0 ? '+' : '') + up.rev_uplift_pct + '%',
            sub: `${applied} applied · ${up.win_rate != null ? up.win_rate + '% win' : ''}`,
            dir: up.rev_uplift_pct >= 0 ? 'up' : 'down' });
    } else {
        cards.push({ lbl: 'Measured Uplift', valTxt: 'Pending', sub: 'apply floors, then re-upload' });
    }

    const host = $('#kpis'); host.innerHTML = '';
    cards.forEach((c, i) => {
        const el = document.createElement('div');
        el.className = 'kpi' + (c.dir ? ' ' + c.dir : '');
        el.style.animationDelay = (i * 70) + 'ms';
        el.innerHTML = `<div class="lbl">${esc(c.lbl)}</div>
            <div class="val">${c.valTxt != null ? esc(c.valTxt) : '—'}</div>
            <div class="sub${c.dir === 'up' ? ' pos' : c.dir === 'down' ? ' neg' : ''}">${esc(c.sub)}</div>`;
        host.appendChild(el);
        if (c.valTxt == null) {
            if (c.val == null) el.querySelector('.val').textContent = '—';
            else countUp(el.querySelector('.val'), c.val, c.pre || '', '', c.dec);
        }
    });
}

/* ── Insights ────────────────────────────────────────────── */
const EMOJI = { chart: '📊', up: '🚀', stable: '⏸️', warning: '⚠️', ai: '🤖' };
function renderInsights(list) {
    const host = $('#insights'); host.innerHTML = '';
    list.forEach((ins, i) => {
        const el = document.createElement('div');
        el.className = 'ins'; el.style.animationDelay = (i * 60) + 'ms';
        el.innerHTML = `<h4><span class="em">${EMOJI[ins.icon] || '•'}</span> ${esc(ins.title)}</h4>
            <ul>${(ins.items || []).map(t => `<li>${esc(t)}</li>`).join('')}</ul>`;
        host.appendChild(el);
    });
}

/* ── Tabs + sliding glider ───────────────────────────────── */
const PANELS = ['basic', 'detailed', 'site', 'trends', 'history'];
function moveGlider(btn, tries = 0) {
    const g = $('#glider'), tabs = $('#tabs').getBoundingClientRect(), b = btn.getBoundingClientRect();
    if (b.width === 0 && tries < 10) { requestAnimationFrame(() => moveGlider(btn, tries + 1)); return; }
    g.style.width = b.width + 'px';
    g.style.transform = `translateX(${b.left - tabs.left - 5}px)`;
}
$$('#tabs .tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
window.addEventListener('resize', () => { const a = $('#tabs .tab.active'); if (a) moveGlider(a); });

function switchTab(tab, isInit) {
    S.tab = tab; S.sortCol = null; S.sortDir = 'asc'; S.q = ''; $('#search-in').value = '';
    $$('#tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const btn = $(`#tabs .tab[data-tab="${tab}"]`); if (btn) { moveGlider(btn); requestAnimationFrame(() => moveGlider(btn)); }
    PANELS.forEach(p => $('#panel-' + p).classList.toggle('on', p === tab));
    toggle('#search', tab === 'basic' || tab === 'detailed');
    $('#btn-dl').style.display = (tab === 'basic' || tab === 'detailed') ? 'inline-flex' : 'none';

    if (tab === 'basic' || tab === 'detailed') renderTable();
    else if (tab === 'site') renderSite();
    else if (tab === 'trends') renderTrends();
    else if (tab === 'history') loadHistory();
}

/* ── Tables (basic / detailed) ───────────────────────────── */
const COLS_BASIC = [
    ['ad_unit', 'Ad Unit'], ['country', 'Country'], ['device', 'Device'],
    ['ecpm', 'eCPM'], ['rpm', 'RPM'], ['current_floor', 'Current'], ['suggested_floor', 'Suggested'],
    ['change_direction', 'Dir'], ['change_pct', 'Δ%'], ['confidence', 'Conf'], ['reason', 'Reason']
];
const COLS_DET = [
    ['ad_unit', 'Ad Unit'], ['country', 'Country'], ['device', 'Device'], ['browser', 'Browser'], ['os', 'OS'],
    ['ecpm', 'eCPM'], ['rpm', 'RPM'], ['current_floor', 'Current'], ['suggested_floor', 'Suggested'],
    ['change_direction', 'Dir'], ['change_pct', 'Δ%'], ['confidence', 'Conf'], ['reason', 'Reason']
];
const NUMCOLS = new Set(['ecpm', 'rpm', 'current_floor', 'suggested_floor', 'change_pct']);

$('#search-in').addEventListener('input', e => { S.q = e.target.value.toLowerCase().trim(); renderTable(); });

function renderTable() {
    const isB = S.tab === 'basic';
    const cols = isB ? COLS_BASIC : COLS_DET;
    const table = $(isB ? '#t-basic' : '#t-detailed');
    let rows = (isB ? S.basic : S.detailed).slice();

    if (S.q) rows = rows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(S.q)));
    if (S.sortCol) {
        const dir = S.sortDir === 'asc' ? 1 : -1;
        rows.sort((a, b) => {
            let x = a[S.sortCol], y = b[S.sortCol];
            if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir;
            x = String(x ?? '').toLowerCase(); y = String(y ?? '').toLowerCase();
            return x < y ? -dir : x > y ? dir : 0;
        });
    }

    // header
    const thead = `<thead><tr>${cols.map(([k, l]) => {
        const sc = k === 'reason' ? '' : 's' + (S.sortCol === k ? ' ' + S.sortDir : '');
        const cls = NUMCOLS.has(k) ? 'num ' + sc : sc;
        return `<th class="${cls.trim()}" data-k="${k}">${l}</th>`;
    }).join('')}</tr></thead>`;

    let body;
    if (!rows.length) {
        body = `<tbody><tr><td class="empty" colspan="${cols.length}">${S.q ? 'No matches for “' + esc(S.q) + '”' : 'No actionable recommendations.'}</td></tr></tbody>`;
    } else {
        const STAGGER = 40; // only animate first N rows for snappiness
        body = '<tbody>' + rows.map((r, i) => {
            const dir = r.change_direction;
            const lft = dir === 'Increase' ? 'lft-inc' : dir === 'Decrease' ? 'lft-dec' : '';
            const anim = i < STAGGER ? ` style="animation-delay:${i * 18}ms"` : '';
            const cls = (i < STAGGER ? 'tr-in ' : '') + lft;
            return `<tr class="${cls.trim()}"${anim}>` + cols.map(([k]) => cell(r, k)).join('') + '</tr>';
        }).join('') + '</tbody>';
    }
    table.innerHTML = thead + body;

    table.querySelectorAll('th.s').forEach(th => th.addEventListener('click', () => {
        const k = th.dataset.k;
        if (S.sortCol === k) S.sortDir = S.sortDir === 'asc' ? 'desc' : 'asc';
        else { S.sortCol = k; S.sortDir = 'asc'; }
        renderTable();
    }));
}

function cell(r, k) {
    if (k === 'ad_unit') return `<td class="au" title="${esc(r.ad_unit)}">${esc(r.ad_unit)}</td>`;
    if (k === 'reason')  return `<td class="reason" title="${esc(r.reason)}">${esc(r.reason)}</td>`;
    if (k === 'ecpm')    return `<td class="num">${r.ecpm != null ? '$' + n2(r.ecpm) : '—'}</td>`;
    if (k === 'rpm')     return `<td class="num">${r.rpm ? '$' + n2(r.rpm) : '—'}</td>`;
    if (k === 'current_floor' || k === 'suggested_floor') return `<td class="num">$${n2(r[k])}</td>`;
    if (k === 'change_pct') return `<td class="num">${r.change_pct != null ? r.change_pct.toFixed(1) + '%' : '—'}</td>`;
    if (k === 'change_direction') {
        const c = r.change_direction === 'Increase' ? 'inc' : r.change_direction === 'Decrease' ? 'dec' : 'nc';
        return `<td><span class="bdg ${c}">${esc(r.change_direction)}</span></td>`;
    }
    if (k === 'confidence') return `<td><span class="conf-${esc(r.confidence)}">${esc(r.confidence)}</span></td>`;
    return `<td>${esc(r[k])}</td>`;
}

/* ── Export CSV ──────────────────────────────────────────── */
$('#btn-dl').addEventListener('click', async () => {
    try {
        const r = await fetch('/api/download', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ basic: S.basic, detailed: S.detailed })
        });
        if (!r.ok) throw new Error((await r.json()).error || 'Export failed');
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement('a');
        a.href = url; a.download = 'floor_suggestions.csv';
        document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { showErr(e.message); }
});

/* ── Site performance ────────────────────────────────────── */
function renderSite() {
    const sp = S.site; if (!sp) return;
    const hasReq = !!sp.has_requests;
    const kpis = [
        { lbl: 'Network Revenue', val: money(sp.total_revenue) },
        { lbl: 'Avg eCPM', val: '$' + n2(sp.avg_ecpm) },
        { lbl: 'Avg RPM', val: hasReq ? '$' + n2(sp.avg_rpm) : '—' },
        { lbl: 'Sites', val: sp.analyzed_sites }
    ];
    $('#site-kpis').innerHTML = kpis.map((k, i) =>
        `<div class="kpi" style="animation-delay:${i * 60}ms"><div class="lbl">${k.lbl}</div><div class="val" style="font-size:1.5rem">${esc(k.val)}</div></div>`).join('');

    const head = `<thead><tr>
        <th>Site / Tag</th><th class="num">Imps</th><th class="num">Revenue</th>
        <th class="num">eCPM</th><th class="num">RPM</th><th>Revenue share</th><th>Recommendation</th>
    </tr></thead>`;
    const body = '<tbody>' + (sp.sites || []).map((s, i) => {
        const rec = s.net_recommendation || 'Hold';
        const rc = rec === 'Raise floors' ? 'inc' : rec === 'Lower floors' ? 'dec' : 'nc';
        const anim = i < 40 ? ` class="tr-in" style="animation-delay:${i * 16}ms"` : '';
        return `<tr${anim}>
            <td class="au" title="${esc(s.site)}">${esc(s.site)}</td>
            <td class="num">${intc(s.total_imps)}</td>
            <td class="num" style="font-weight:700">${money(s.total_rev)}</td>
            <td class="num">$${n2(s.ecpm)}</td>
            <td class="num">${hasReq ? '$' + n2(s.rpm) : '—'}</td>
            <td><div class="revcell"><div class="revbar"><i data-w="${s.rev_share}"></i></div><span>${s.rev_share}%</span></div></td>
            <td><span class="bdg ${rc}">${esc(rec)}</span></td>
        </tr>`;
    }).join('') + '</tbody>';
    $('#t-site').innerHTML = head + body;
    // animate revenue bars
    requestAnimationFrame(() => $$('#t-site .revbar i').forEach(b => { b.style.width = (b.dataset.w || 0) + '%'; }));
}

/* ── Day-of-week trends ──────────────────────────────────── */
function barRow(d, best, worst) {
    const max = best.max || 1;
    const cls = d.day_of_week === best.day ? 'best' : d.day_of_week === worst.day ? 'worst' : '';
    const pct = max > 0 ? (d.ecpm / max * 100) : 0;
    return `<div class="row"><div class="day">${esc(d.day_of_week)}</div>
        <div class="track"><div class="fill ${cls}" data-w="${pct}"></div></div>
        <div class="v">$${n2(d.ecpm)} <span style="color:var(--text-3)">($${intc(Math.round(d.total_rev))})</span></div></div>`;
}
function animateBars(scope) { requestAnimationFrame(() => $$(scope + ' .fill').forEach(f => f.style.width = (f.dataset.w || 0) + '%')); }

function renderTrends() {
    const t = S.trends;
    if (!t || !t.network || !t.network.length) {
        $('#panel-trends').querySelector('.trend-grid').innerHTML =
            `<div class="card" style="grid-column:1/-1"><div class="empty">No date / day-of-week column found in this report.</div></div>`;
        $('#t-ww').innerHTML = ''; return;
    }
    const best = { day: '', max: -1 }, worst = { day: '', max: Infinity };
    t.network.forEach(d => { if (d.ecpm > best.max) { best.max = d.ecpm; best.day = d.day_of_week; } if (d.ecpm < worst.max) { worst.max = d.ecpm; worst.day = d.day_of_week; } });

    $('#net-bars').innerHTML = t.network.map(d => barRow(d, best, worst)).join('');
    animateBars('#net-bars');
    const ni = t.insights || {};
    $('#net-note').innerHTML = ni.best_day_ecpm
        ? `☀️ Peak eCPM <b>${esc(ni.best_day_ecpm)}</b> at <b>$${n2(ni.best_ecpm_val)}</b> · 📉 lowest <b>${esc(ni.worst_day_ecpm)}</b> ($${n2(ni.worst_ecpm_val)}) · 💰 top revenue day <b>${esc(ni.best_day_rev)}</b>.`
        : 'Daily patterns across the reporting window.';

    // site selector
    const sel = $('#site-sel'); const sites = Object.keys(t.sites || {});
    sel.innerHTML = sites.map(s => `<option>${esc(s)}</option>`).join('');
    sel.onchange = () => renderSiteTrend(sel.value);
    if (sites.length) renderSiteTrend(sel.value || sites[0]);

    // weekday/weekend table
    const head = `<thead><tr><th>Site</th><th class="num">Weekday eCPM</th><th class="num">Weekend eCPM</th>
        <th>Variance</th><th class="num">Weekday rev</th><th class="num">Weekend rev</th><th>Action</th></tr></thead>`;
    const body = '<tbody>' + (t.weekday_weekend || []).map(r => {
        const vc = r.variance > 10 ? 'inc' : r.variance < -10 ? 'dec' : 'nc';
        return `<tr><td class="au" title="${esc(r.site)}">${esc(r.site)}</td>
            <td class="num">$${n2(r.weekday_ecpm)}</td><td class="num">$${n2(r.weekend_ecpm)}</td>
            <td><span class="bdg ${vc}">${r.variance > 0 ? '+' : ''}${r.variance}%</span></td>
            <td class="num">${money(r.weekday_rev)} (${r.weekday_share}%)</td>
            <td class="num">${money(r.weekend_rev)} (${r.weekend_share}%)</td>
            <td style="white-space:normal;font-size:.75rem;color:var(--text-2)">${esc(r.strategy)}</td></tr>`;
    }).join('') + '</tbody>';
    $('#t-ww').innerHTML = head + body;
}

function renderSiteTrend(site) {
    const t = S.trends; if (!t || !t.sites[site]) return;
    const data = t.sites[site];
    const best = { day: '', max: -1 }, worst = { day: '', max: Infinity };
    data.forEach(d => { if (d.ecpm > best.max) { best.max = d.ecpm; best.day = d.day_of_week; } if (d.ecpm < worst.max) { worst.max = d.ecpm; worst.day = d.day_of_week; } });
    $('#site-bars').innerHTML = data.map(d => barRow(d, best, worst)).join('');
    animateBars('#site-bars');
    const ww = (t.weekday_weekend || []).find(s => s.site === site);
    let note = `Best day <b>${esc(best.day)}</b> ($${n2(best.max)}).`;
    if (ww) note += ww.variance < -10 ? ` ⚠️ Weekend ${Math.abs(ww.variance)}% lower — discount weekend floors.`
        : ww.variance > 10 ? ` 🚀 Weekend ${ww.variance}% higher — premium weekend floors.`
        : ` ⚖️ Stable (${ww.variance}%) — uniform floors.`;
    $('#site-note').innerHTML = note;
}

/* ── History ─────────────────────────────────────────────── */
async function loadHistory() {
    const host = $('#history');
    host.innerHTML = '<p style="color:var(--text-2)">Loading…</p>';
    try {
        const d = await (await fetch('/api/history')).json();
        if (!d.uploads || !d.uploads.length) { host.innerHTML = '<div class="empty">No uploads yet. Analyze a report to start the learning loop.</div>'; return; }
        const l = d.learning || {};
        const acc = l.accuracy == null ? 'Pending' : (l.accuracy * 100).toFixed(0) + '%';
        const kpis = [
            { lbl: 'Total Uploads', v: d.uploads.length },
            { lbl: 'Training Rows', v: intc(d.total_rows) },
            { lbl: 'Status', v: d.uploads.length >= 3 ? '● Active' : '○ Learning' },
            { lbl: 'Outcome Accuracy', v: acc, sub: `${intc(l.applied_outcomes)} applied` },
        ];
        let h = `<div class="kpis" style="margin-top:0">` + kpis.map((k, i) =>
            `<div class="kpi" style="animation-delay:${i * 60}ms"><div class="lbl">${k.lbl}</div><div class="val" style="font-size:1.4rem">${esc(k.v)}</div>${k.sub ? `<div class="sub">${esc(k.sub)}</div>` : ''}</div>`).join('') + `</div>`;

        h += `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>#</th><th>File</th><th>Date</th><th class="num">Rows</th><th class="num">Recs</th></tr></thead><tbody>`;
        d.uploads.forEach(u => h += `<tr><td>${u.id}</td><td class="au">${esc(u.filename)}</td><td>${esc(String(u.timestamp))}</td><td class="num">${intc(u.rows_processed)}</td><td class="num" style="color:var(--indigo);font-weight:700">${intc(u.rec_count)}</td></tr>`);
        h += '</tbody></table></div>';

        if (l.confusion_matrix && l.confusion_matrix.length) {
            h += `<h3 class="sec-title" style="margin-top:22px">Outcome matrix</h3><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Predicted</th><th>Observed</th><th class="num">Count</th></tr></thead><tbody>`;
            l.confusion_matrix.forEach(r => h += `<tr><td>${esc(r.predicted_direction)}</td><td>${esc(r.actual_direction)}</td><td class="num" style="color:var(--indigo);font-weight:700">${intc(r.n)}</td></tr>`);
            h += '</tbody></table></div>';
        }
        host.innerHTML = h;
    } catch (e) { host.innerHTML = `<p style="color:var(--red)">Failed to load history: ${esc(e.message)}</p>`; }
}
