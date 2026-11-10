// VPS Traffic Panel front end (loaded at the end of index.html).
function formatBytes(bytes, isSpeed=false) {
    if (!bytes || bytes === 0) return '0 B' + (isSpeed ? '/s' : '');
    const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.max(0, Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k))));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i] + (isSpeed ? '/s' : '');
}

// Live speed with at most 3 significant digits ("238", "14.0", "1.95"), so the number
// never grows past 4 characters and the two columns cannot run into each other.
let speedUnit = 'bytes';
try { speedUnit = localStorage.getItem('speedUnit') === 'bits' ? 'bits' : 'bytes'; } catch (e) { /* storage blocked */ }
const lastSpeed = { 'rx-speed': 0, 'tx-speed': 0 };

function formatSpeed(bytes, unitMode = speedUnit) {
    const bits = unitMode === 'bits';
    // Bandwidth is usually quoted in decimal bits per second (1 Mbps = 1,000,000 bit/s).
    let value = Math.max(0, Number(bytes) || 0) * (bits ? 8 : 1);
    const units = bits ? ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'] : ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s'];
    const base = bits ? 1000 : 1024;
    const show = (v, unitIndex) => v === 0 ? '0'
        : v >= 100 || unitIndex === 0 ? v.toFixed(0)
        : v >= 10 ? v.toFixed(1)
        : v.toFixed(2);
    let i = 0;
    // Move up a unit when the rounded number would reach 1000 (e.g. 999.9 KB/s -> 0.98 MB/s).
    while (Number(show(value, i)) >= 1000 && i < units.length - 1) {
        value /= base;
        i++;
    }
    return { num: show(value, i), unit: units[i] };
}

function toggleSpeedUnit() {
    speedUnit = speedUnit === 'bits' ? 'bytes' : 'bits';
    try { localStorage.setItem('speedUnit', speedUnit); } catch (e) { /* storage blocked */ }
    setSpeed('rx-speed', lastSpeed['rx-speed']);
    setSpeed('tx-speed', lastSpeed['tx-speed']);
}

function setSpeed(id, bytes) {
    lastSpeed[id] = bytes;
    const el = document.getElementById(id);
    const { num, unit } = formatSpeed(bytes);
    el.querySelector('.speed-num').textContent = num;
    el.querySelector('.speed-unit').textContent = unit;
    el.title = `${formatBytes(bytes, true)} · ${(Math.max(0, Number(bytes) || 0) * 8 / 1e6).toFixed(2)} Mbps`;
}

function bytesToQuotaInput(bytes) {
    const value = Number(bytes) || 0;
    const TB = 1099511627776;
    if (value >= TB) {
        // Show TB when the stored bytes came from a TB value with up to 2 decimals (e.g. 1.95 TB).
        const tb = Number((value / TB).toFixed(2));
        if (Math.round(tb * TB) === value) return { value: tb, unit: 'TB' };
    }
    return { value: value ? Number((value / 1073741824).toFixed(2)) : '', unit: 'GB' };
}

function quotaInputToBytes() {
    const value = Number(document.getElementById('traffic-quota-value').value) || 0;
    const unit = document.getElementById('traffic-quota-unit').value;
    return Math.round(value * (unit === 'TB' ? 1099511627776 : 1073741824));
}

function setProgress(id, value) {
    const percent = Math.max(0, Math.min(100, Number(value) || 0));
    document.getElementById(id).style.width = `${percent}%`;
}

function renderTrafficQuota(quota) {
    quota = quota || {};
    const track = document.getElementById('traffic-quota-track');
    const bar = document.getElementById('traffic-quota-bar');
    const percentEl = document.getElementById('traffic-quota-percent');
    const detail = document.getElementById('traffic-quota-detail');
    const resetEl = document.getElementById('traffic-quota-reset');
    if (quota.days_left !== null && quota.days_left !== undefined && quota.reset_date) {
        resetEl.textContent = `距離重置 ${quota.days_left} 天（${quota.reset_date}）`;
        resetEl.classList.remove('hidden');
    } else {
        resetEl.classList.add('hidden');
    }
    if (!quota.available) {
        track.classList.add('hidden');
        percentEl.textContent = '';
        detail.textContent = '未設定';
        detail.title = quota.error || '';
        return;
    }
    // Real percentage in the label (can exceed 100%), bar capped at full width.
    const percent = Math.max(0, Number(quota.percent) || 0);
    const tone = percent >= 90 ? ['bg-red-400', 'text-red-400']
        : percent >= 70 ? ['bg-amber-400', 'text-amber-400']
        : ['bg-emerald-400', 'text-emerald-400'];
    track.classList.remove('hidden');
    // Keep a sliver visible once anything is used, so 0.3% doesn't look like an empty bar.
    bar.style.width = `${Number(quota.used) > 0 ? Math.min(100, Math.max(percent, 1)) : 0}%`;
    bar.className = `progress-fill ${tone[0]}`;
    percentEl.className = `ml-1 font-mono font-semibold ${tone[1]}`;
    percentEl.textContent = `${percent.toFixed(1)}%`;
    detail.textContent = `${formatBytes(quota.used)} / ${formatBytes(quota.limit)}`;
    detail.title = quota.source_label ? `數據來源：${quota.source_label}` : '';
}

function renderLoadAverage(load, cores) {
    const el = document.getElementById('sys-load');
    if (!Array.isArray(load) || load.length !== 3) {
        el.textContent = '';
        return;
    }
    el.textContent = `負載 ${load.map(v => Number(v).toFixed(2)).join(' / ')}`;
    // Judge by the 5-minute average relative to core count, so a short spike doesn't flash red.
    const ratio = load[1] / Math.max(1, Number(cores) || 1);
    el.className = `mt-0.5 font-mono whitespace-nowrap ${ratio >= 2 ? 'text-red-400' : ratio >= 1 ? 'text-amber-400' : 'text-slate-500'}`;
}

function renderVpsExpiry(expiry) {
    const row = document.getElementById('vps-expiry-row');
    const el = document.getElementById('vps-expiry');
    if (!expiry || !expiry.date) {
        row.classList.add('hidden');
        return;
    }
    const days = Number(expiry.days_left);
    // Break only between the date and the status so "（剩 362 天）" never splits.
    const status = document.createElement('span');
    status.className = 'whitespace-nowrap';
    status.textContent = days < 0 ? `（已過期 ${-days} 天）` : days === 0 ? '（今天到期）' : `（剩 ${days} 天）`;
    el.replaceChildren(document.createTextNode(expiry.date), document.createElement('wbr'), status);
    // An expired VPS takes the panel down with it, so warn early: amber at 14 days, red at 7.
    el.className = `font-mono ${days <= 7 ? 'text-red-400 font-semibold' : days <= 14 ? 'text-amber-400' : 'text-slate-200'}`;
    row.classList.remove('hidden');
}

let sslStatus = null;

function renderSslStatus(data) {
    sslStatus = data;
    const summary = document.getElementById('ssl-summary');
    let text = '未啟用（HTTP）';
    let tone = 'text-slate-400';
    if (data.enabled) {
        const days = data.days_left;
        if (!data.reachable) {
            text = '無法連線';
            tone = 'text-red-400';
        } else if (!data.trusted) {
            text = '證書有問題';
            tone = 'text-red-400';
        } else {
            text = `有效（剩 ${days} 天）`;
            tone = days <= 7 ? 'text-red-400' : days <= 14 ? 'text-amber-400' : 'text-emerald-400';
        }
    }
    summary.textContent = text;
    summary.className = `font-mono underline decoration-dotted underline-offset-2 hover:text-slate-200 ${tone}`;
    renderSslDetails(data);
    updateSslSwitchVisibility(data);
}

function renderSslDetails(data) {
    const box = document.getElementById('ssl-details');
    const rows = [];
    if (!data.enabled) {
        rows.push(['狀態', '未啟用 HTTPS，面板目前以 HTTP 提供服務', 'text-amber-300']);
        rows.push(['啟用方式', '在 VPS 執行 vps-panel，選擇重新安裝並使用證書', '']);
    } else {
        const state = !data.reachable ? `無法連線：${data.error}` : data.trusted ? '有效，瀏覽器會信任' : `有問題：${data.error}`;
        rows.push(['狀態', state, data.trusted ? 'text-emerald-300' : 'text-red-300']);
        rows.push(['面板網域', data.domain || '（以 IP 直連）', '']);
        rows.push(['證書網域', [data.subject, ...(data.san || []).filter(n => n !== data.subject)].filter(Boolean).join('、') || '未知', '']);
        rows.push(['簽發者', data.issuer || '未知', '']);
        if (data.not_after) {
            const days = data.days_left;
            rows.push(['到期日', `${data.not_after}（${days < 0 ? `已過期 ${-days} 天` : `剩 ${days} 天`}）`, days <= 7 ? 'text-red-300' : days <= 14 ? 'text-amber-300' : '']);
        }
        rows.push(['HTTPS 模式', data.mode === 'nginx' ? `Nginx 反向代理，端口 ${data.port}` : `面板直連 HTTPS，端口 ${data.port}`, '']);
        if (data.cert_file) rows.push(['證書檔案', data.cert_file, '']);
        if (data.renewal_watch !== null && data.renewal_watch !== undefined) {
            rows.push(['續期監看', data.renewal_watch ? '運行中：證書更新後自動套用' : '未運行（執行 vps-panel update 可啟用）', data.renewal_watch ? '' : 'text-amber-300']);
        }
    }
    box.replaceChildren(...rows.map(([label, value, cls]) => {
        const p = document.createElement('p');
        const l = document.createElement('span');
        l.className = 'text-slate-500';
        l.textContent = `${label}：`;
        const v = document.createElement('span');
        v.className = `break-all ${cls || 'text-slate-200'}`;
        v.textContent = value;
        p.append(l, v);
        return p;
    }));
    document.getElementById('ssl-expiry-notify').checked = data.ssl_expiry_notify !== false;
}

async function fetchSslStatus(refresh = false) {
    try {
        const res = await fetch(`/api/ssl-status${refresh ? '?refresh=1' : ''}`);
        if (!res.ok) throw new Error(res.statusText);
        renderSslStatus(await res.json());
    } catch (e) {
        document.getElementById('ssl-summary').textContent = '讀取失敗';
        console.error("Error fetching /api/ssl-status:", e);
    }
}

let sslCandidates = [];

function sslBadge(text, tone) {
    const b = document.createElement('span');
    b.className = `rounded border px-1.5 py-0.5 text-[10px] ${tone}`;
    b.textContent = text;
    return b;
}

function renderSslCandidates(list, domain) {
    sslCandidates = list;
    const box = document.getElementById('ssl-candidates');
    const applyBtn = document.getElementById('ssl-apply');
    box.replaceChildren();
    applyBtn.disabled = true;
    applyBtn.classList.toggle('hidden', list.length === 0);
    list.forEach((c) => {
        const row = document.createElement('label');
        row.className = `block rounded border p-2.5 text-xs ${c.usable && !c.current ? 'border-slate-600 hover:border-sky-400 cursor-pointer' : 'border-slate-700 opacity-80'}`;
        const head = document.createElement('div');
        head.className = 'flex flex-wrap items-center gap-1.5';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'ssl-candidate';
        radio.value = c.id;
        radio.className = 'accent-sky-400';
        radio.disabled = !c.usable || c.current;
        radio.addEventListener('change', () => { applyBtn.disabled = false; });
        const title = document.createElement('span');
        title.className = 'font-mono font-semibold text-slate-100 break-all';
        title.textContent = c.subject || (c.names || [])[0] || '（無名稱）';
        head.append(radio, title);
        if (c.current) head.append(sslBadge('使用中', 'border-emerald-500/60 text-emerald-300'));
        if (c.matches_domain === true) head.append(sslBadge(`符合 ${domain}`, 'border-sky-500/60 text-sky-300'));
        if (c.self_signed) head.append(sslBadge('自簽', 'border-amber-500/60 text-amber-300'));
        (c.problems || []).forEach((p) => head.append(sslBadge(p, 'border-red-500/60 text-red-300')));
        const meta = document.createElement('p');
        meta.className = 'mt-1 text-slate-400';
        const days = c.days_left;
        meta.textContent = `${c.issuer || '未知簽發者'} · 到期 ${c.not_after || '未知'}${days !== null && days !== undefined ? `（${days < 0 ? `已過期 ${-days} 天` : `剩 ${days} 天`}）` : ''}`;
        const names = document.createElement('p');
        names.className = 'text-slate-500 break-all';
        names.textContent = (c.names || []).length > 1 ? `網域：${c.names.join('、')}` : '';
        const path = document.createElement('p');
        path.className = 'mt-1 font-mono text-[10px] text-slate-500 break-all';
        path.textContent = `${c.cert}　${c.label || ''}`;
        row.append(head, meta, names, path);
        box.appendChild(row);
    });
}

async function sslScan() {
    const status = document.getElementById('ssl-switch-status');
    const btn = document.getElementById('ssl-scan');
    btn.disabled = true;
    status.textContent = '正在搜尋本機證書，可能需要幾秒鐘...';
    try {
        const res = await fetch('/api/ssl/scan', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
        renderSslCandidates(data.candidates || [], data.domain || '');
        const usable = (data.candidates || []).filter(c => c.usable && !c.current).length;
        status.textContent = data.candidates.length
            ? `找到 ${data.candidates.length} 張證書，其中 ${usable} 張可以切換。`
            : '沒有找到其他證書。';
    } catch (e) {
        status.textContent = `搜尋失敗：${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

async function sslApply() {
    const selected = document.querySelector('input[name="ssl-candidate"]:checked');
    if (!selected) return;
    const c = sslCandidates.find(item => item.id === selected.value);
    const status = document.getElementById('ssl-switch-status');
    const directMode = sslStatus && sslStatus.mode === 'uvicorn';
    const warning = directMode ? '\n\n面板會重新啟動，頁面可能短暫中斷。' : '';
    if (!confirm(`確定改用這張證書？\n\n${c.subject}\n${c.cert}\n到期 ${c.not_after}${c.self_signed ? '\n\n注意：這是自簽證書，瀏覽器會顯示不安全。' : ''}${warning}`)) return;
    const applyBtn = document.getElementById('ssl-apply');
    applyBtn.disabled = true;
    status.textContent = '正在套用並驗證...';
    try {
        const res = await fetch('/api/ssl/apply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ candidate: c.id })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
        renderSslStatus(Object.assign({ ssl_expiry_notify: document.getElementById('ssl-expiry-notify').checked }, data.ssl));
        status.textContent = `已切換為 ${c.subject}，並確認面板正在使用新證書。`;
        renderSslCandidates([], '');
    } catch (e) {
        if (directMode && e instanceof TypeError) {
            // Direct HTTPS mode restarts the panel itself; check again once it is back.
            status.textContent = '面板正在重新啟動，稍後自動重新檢查...';
            setTimeout(() => fetchSslStatus(true).then(() => { status.textContent = '已重新檢查，請確認上方證書資訊。'; }), 8000);
        } else {
            status.textContent = `切換失敗：${e.message}`;
            applyBtn.disabled = false;
        }
    }
}

function updateSslSwitchVisibility(data) {
    document.getElementById('ssl-switch').classList.toggle('hidden', !data.enabled);
}

async function saveSslNotify() {
    try {
        await fetch('/api/ssl-settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ssl_expiry_notify: document.getElementById('ssl-expiry-notify').checked })
        });
    } catch (e) {
        console.error("Error saving /api/ssl-settings:", e);
    }
}

function renderBandwagonPlan(bw) {
    const planRow = document.getElementById('bw-plan-row');
    const suspendedRow = document.getElementById('bw-suspended-row');
    if (!bw) {
        planRow.classList.add('hidden');
        suspendedRow.classList.add('hidden');
        return;
    }
    const parts = [];
    if (bw.plan_ram) parts.push(`${formatBytes(bw.plan_ram)} 記憶體`);
    if (bw.plan_disk) parts.push(`${formatBytes(bw.plan_disk)} 硬碟`);
    if (bw.plan_swap) parts.push(`${formatBytes(bw.plan_swap)} Swap`);
    if (bw.multiplier && bw.multiplier > 1) parts.push(`流量 ×${bw.multiplier}`);
    document.getElementById('bw-plan').textContent = parts.join(' · ');
    planRow.classList.toggle('hidden', parts.length === 0);
    suspendedRow.classList.toggle('hidden', !bw.suspended);
}

async function restartVps() {
    const status = document.getElementById('restart-status');
    const btn = document.getElementById('vps-restart');
    if (!confirm('確定要重啟整台 VPS 嗎？\n\n所有服務會中斷約 1 分鐘，未儲存的資料可能遺失。')) return;
    btn.disabled = true;
    status.textContent = '正在送出重啟指令...';
    try {
        await saveBandwagonSettings();
        const res = await fetch('/api/bandwagon/restart', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
    } catch (e) {
        status.textContent = `重啟失敗：${e.message}`;
        btn.disabled = false;
        return;
    }
    status.textContent = '已送出重啟指令，等待 VPS 恢復...';
    const started = Date.now();
    await new Promise(r => setTimeout(r, 15000));
    while (Date.now() - started < 5 * 60 * 1000) {
        try {
            const res = await fetch(`/api/ping?t=${Date.now()}`, { cache: 'no-store' });
            if (res.ok) {
                status.textContent = `VPS 已恢復（約 ${Math.round((Date.now() - started) / 1000)} 秒）。`;
                btn.disabled = false;
                fetchSystem();
                return;
            }
        } catch (e) { /* still rebooting */ }
        await new Promise(r => setTimeout(r, 5000));
    }
    status.textContent = '5 分鐘內未恢復，請到 KiwiVM 控制台查看狀態。';
    btn.disabled = false;
}

async function openInterfacePicker() {
    openModal('iface-modal');
    const list = document.getElementById('iface-list');
    list.textContent = '讀取中...';
    try {
        const res = await fetch('/api/interfaces');
        const data = await res.json();
        const option = (value, title, detail, checked, badges) => {
            const row = document.createElement('label');
            row.className = 'flex items-start gap-2 rounded border border-slate-700 hover:border-emerald-400 p-2.5 text-xs cursor-pointer';
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = 'iface';
            radio.value = value;
            radio.checked = checked;
            radio.className = 'mt-0.5 accent-emerald-400';
            const text = document.createElement('div');
            const head = document.createElement('p');
            head.className = 'font-mono font-semibold text-slate-100';
            head.textContent = title;
            badges.forEach(([label, tone]) => {
                const b = document.createElement('span');
                b.className = `ml-2 rounded border px-1.5 py-0.5 text-[10px] font-normal ${tone}`;
                b.textContent = label;
                head.appendChild(b);
            });
            const sub = document.createElement('p');
            sub.className = 'mt-0.5 text-slate-500';
            sub.textContent = detail;
            text.append(head, sub);
            row.append(radio, text);
            return row;
        };
        const rows = [option('', '自動偵測（建議）', `跟隨預設路由，目前是 ${data.current}`, !data.pinned, [])];
        data.interfaces.forEach((iface) => {
            const badges = [];
            if (iface.current) badges.push(['使用中', 'border-emerald-500/60 text-emerald-300']);
            if (iface.default) badges.push(['預設路由', 'border-sky-500/60 text-sky-300']);
            rows.push(option(iface.name, iface.name, `開機以來 ↓ ${formatBytes(iface.rx)} · ↑ ${formatBytes(iface.tx)}`, data.pinned === iface.name, badges));
        });
        list.replaceChildren(...rows);
    } catch (e) {
        list.textContent = '讀取網卡失敗';
    }
}

async function saveInterface() {
    const selected = document.querySelector('input[name="iface"]:checked');
    const status = document.getElementById('iface-status');
    if (!selected) return;
    try {
        const res = await fetch('/api/interface', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ interface: selected.value })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
        document.getElementById('footer-iface').textContent = data.current;
        closeModal('iface-modal');
        fetchSystem();
    } catch (e) {
        status.textContent = `保存失敗：${e.message}`;
    }
}

function formatCoordinates(lat, lon) {
    if (lat === null || lat === undefined || lon === null || lon === undefined) return 'Unknown';
    return `${Number(lat).toFixed(4)}, ${Number(lon).toFixed(4)}`;
}

// Every API call carries this header; the server rejects POSTs without it (CSRF guard).
const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
    const headers = new Headers(init.headers || {});
    headers.set('X-Panel-Request', '1');
    const response = await nativeFetch(input, { ...init, headers, credentials: 'same-origin' });
    // Session expired or signed out elsewhere: back to the login page.
    if (response.status === 401) window.location.href = '/login';
    return response;
};

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
}

async function fetchSettings() {
    try {
        const res = await fetch('/api/settings');
        const data = await res.json();
        document.getElementById('settings-username').value = data.auth_username;
        document.getElementById('settings-title').value = data.panel_title;
        document.getElementById('settings-subtitle').value = data.panel_subtitle;
        document.getElementById('panel-title').innerText = data.panel_title;
        document.getElementById('panel-subtitle').innerText = data.panel_subtitle;
        document.title = data.panel_title;
    } catch (e) {
        console.error("Error fetching /api/settings:", e);
    }
}

function openModal(id) {
    document.getElementById(id).classList.add('active');
}

function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}

function buildResetDayOptions() {
    const select = document.getElementById('reset-day-select');
    if (select.options.length > 0) return;
    for (let day = 1; day <= 31; day++) {
        const option = document.createElement('option');
        option.value = String(day);
        option.textContent = `${day} 日`;
        select.appendChild(option);
    }
}

function timezoneDisplay(flag, city, country, offset) {
    const utc = offset >= 0 ? `UTC+${offset}` : `UTC${offset}`;
    return `${flag}  ${city.padEnd(8, ' ')} ${country.padEnd(8, ' ')} ${utc}`;
}

function buildTelegramScheduleOptions() {
    const timezone = document.getElementById('telegram-timezone');
    if (timezone.options.length === 0) {
        const zones = [
            [-12, timezoneDisplay('🇺🇸', '貝克島', '美國', -12), '貝克島時間'],
            [-11, timezoneDisplay('🇼🇸', '阿皮亞', '薩摩亞', -11), '薩摩亞時間'],
            [-10, timezoneDisplay('🇺🇸', '檀香山', '美國', -10), '夏威夷時間'],
            [-9, timezoneDisplay('🇺🇸', '安克雷奇', '美國', -9), '阿拉斯加時間'],
            [-8, timezoneDisplay('🇺🇸', '洛杉磯', '美國', -8), '太平洋時間'],
            [-7, timezoneDisplay('🇺🇸', '丹佛', '美國', -7), '山區時間'],
            [-6, timezoneDisplay('🇺🇸', '芝加哥', '美國', -6), '中部時間'],
            [-5, timezoneDisplay('🇺🇸', '紐約', '美國', -5), '東部時間'],
            [-4, timezoneDisplay('🇨🇱', '聖地牙哥', '智利', -4), '智利時間'],
            [-3, timezoneDisplay('🇧🇷', '聖保羅', '巴西', -3), '巴西時間'],
            [-2, timezoneDisplay('🇬🇸', '古利德維肯', '南喬治亞', -2), '南喬治亞時間'],
            [-1, timezoneDisplay('🇨🇻', '普拉亞', '維德角', -1), '維德角時間'],
            [0, timezoneDisplay('🇬🇧', '倫敦', '英國', 0), '英國時間'],
            [1, timezoneDisplay('🇩🇪', '柏林', '德國', 1), '中歐時間'],
            [2, timezoneDisplay('🇿🇦', '約翰尼斯堡', '南非', 2), '南非時間'],
            [3, timezoneDisplay('🇹🇷', '伊斯坦堡', '土耳其', 3), '土耳其時間'],
            [4, timezoneDisplay('🇦🇪', '杜拜', '阿聯酋', 4), '阿聯酋時間'],
            [5, timezoneDisplay('🇵🇰', '卡拉奇', '巴基斯坦', 5), '巴基斯坦時間'],
            [6, timezoneDisplay('🇧🇩', '達卡', '孟加拉', 6), '孟加拉時間'],
            [7, timezoneDisplay('🇹🇭', '曼谷', '泰國', 7), '泰國時間'],
            [8, timezoneDisplay('🇨🇳', '上海', '中國', 8), '中國時間'],
            [9, timezoneDisplay('🇯🇵', '東京', '日本', 9), '日本時間'],
            [10, timezoneDisplay('🇦🇺', '雪梨', '澳洲', 10), '澳洲東部時間'],
            [11, timezoneDisplay('🇸🇧', '霍尼亞拉', '所羅門群島', 11), '所羅門時間'],
            [12, timezoneDisplay('🇳🇿', '奧克蘭', '紐西蘭', 12), '紐西蘭時間'],
            [13, timezoneDisplay('🇹🇴', '努瓜婁發', '東加', 13), '東加時間'],
            [14, timezoneDisplay('🇰🇮', '聖誕島', '基里巴斯', 14), '基里巴斯時間']
        ];
        zones.forEach(([offset, label, shortLabel]) => {
            const option = document.createElement('option');
            option.value = String(offset);
            option.textContent = label;
            option.dataset.label = shortLabel;
            timezone.appendChild(option);
        });
        timezone.value = '8';
    }

    const hourSelect = document.getElementById('telegram-push-hour');
    if (hourSelect.options.length === 0) {
        for (let hour = 0; hour <= 23; hour++) {
            const option = document.createElement('option');
            option.value = String(hour);
            option.textContent = String(hour).padStart(2, '0');
            hourSelect.appendChild(option);
        }
        hourSelect.value = '20';
    }

    const minuteSelect = document.getElementById('telegram-push-minute');
    if (minuteSelect.options.length > 0) return;
    for (let minute = 0; minute <= 59; minute++) {
        const option = document.createElement('option');
        option.value = String(minute);
        option.textContent = String(minute).padStart(2, '0');
        minuteSelect.appendChild(option);
    }
    minuteSelect.value = '0';
}

function selectedTelegramTimezoneLabel() {
    const select = document.getElementById('telegram-timezone');
    return select.options[select.selectedIndex]?.dataset.label || '自訂時區';
}

async function fetchTelegramSettings() {
    try {
        const res = await fetch('/api/telegram-settings');
        const data = await res.json();
        document.getElementById('telegram-enabled').checked = Boolean(data.telegram_enabled);
        document.getElementById('telegram-token').value = data.telegram_bot_token || '';
        document.getElementById('telegram-chat-id').value = data.telegram_chat_id || '';
        document.getElementById('telegram-push-hour').value = String(data.telegram_push_hour ?? 20);
        document.getElementById('telegram-push-minute').value = String(data.telegram_push_minute ?? 0);
        document.getElementById('telegram-timezone').value = String(data.telegram_timezone_offset ?? 8);
        const timezone = document.getElementById('telegram-timezone');
        const selectedOption = timezone.options[timezone.selectedIndex];
        if (selectedOption && data.telegram_timezone_label) selectedOption.dataset.label = data.telegram_timezone_label;
        document.getElementById('bandwagon-veid').value = data.bandwagon_veid || '';
        document.getElementById('bandwagon-api-key').value = data.bandwagon_api_key || '';
    } catch (e) {
        console.error("Error fetching /api/telegram-settings:", e);
    }
}

async function saveTelegramSettings(closeAfterSave = true) {
    const status = document.getElementById('telegram-status');
    const payload = {
        telegram_enabled: document.getElementById('telegram-enabled').checked,
        telegram_bot_token: document.getElementById('telegram-token').value,
        telegram_chat_id: document.getElementById('telegram-chat-id').value,
        telegram_push_hour: Number(document.getElementById('telegram-push-hour').value),
        telegram_push_minute: Number(document.getElementById('telegram-push-minute').value),
        telegram_timezone_offset: Number(document.getElementById('telegram-timezone').value),
        telegram_timezone_label: selectedTelegramTimezoneLabel(),
        bandwagon_veid: document.getElementById('bandwagon-veid').value,
        bandwagon_api_key: document.getElementById('bandwagon-api-key').value
    };
    try {
        const res = await fetch('/api/telegram-settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(await res.text());
        const hour = String(payload.telegram_push_hour).padStart(2, '0');
        const minute = String(payload.telegram_push_minute).padStart(2, '0');
        status.innerText = `已保存。啟用後每天 ${payload.telegram_timezone_label} ${hour}:${minute} 推送流量摘要。`;
        await fetchBandwagonSettings();
        await fetchTrafficQuotaSettings();
        await fetchSummary();
        if (closeAfterSave) closeModal('telegram-modal');
        return true;
    } catch (e) {
        status.innerText = '保存失敗，請檢查 Token、Chat ID 或 Bandwagon 資訊。';
        console.error("Error saving /api/telegram-settings:", e);
        return false;
    }
}

async function testTelegramPush() {
    const status = document.getElementById('telegram-status');
    const saved = await saveTelegramSettings(false);
    if (!saved) return;
    try {
        const res = await fetch('/api/telegram-test', { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        status.innerText = '測試推送已發送。';
    } catch (e) {
        status.innerText = '測試推送失敗，請檢查 Bot Token、Chat ID 是否正確。';
        console.error("Error testing /api/telegram-test:", e);
    }
}

async function fetchDdnsSettings() {
    try {
        const res = await fetch('/api/ddns-settings');
        const data = await res.json();
        document.getElementById('ddns-enabled').checked = Boolean(data.cloudflare_ddns_enabled);
        document.getElementById('ddns-token').value = data.cloudflare_api_token || '';
        document.getElementById('ddns-zone-id').value = data.cloudflare_zone_id || '';
        document.getElementById('ddns-record-name').value = data.cloudflare_record_name || '';
        document.getElementById('ddns-proxied').checked = Boolean(data.cloudflare_proxied);
        document.getElementById('ddns-interval').value = String(data.cloudflare_ddns_interval ?? 5);
        document.getElementById('ddns-ttl').value = String(data.cloudflare_ttl ?? 1);
        if (!document.getElementById('ddns-ttl').value) document.getElementById('ddns-ttl').value = '1';
        document.getElementById('ddns-notify').checked = data.cloudflare_ddns_notify !== false;
        document.getElementById('ddns-notify-hint').innerText = data.telegram_ready
            ? '使用「TG 推送」裡的 Bot Token 與 Chat ID，不必開啟每日推送。'
            : '尚未設定 Telegram Bot Token / Chat ID，請先到「TG 推送」填寫。';
        syncDdnsTtlState();
        renderDdnsState(data);
    } catch (e) {
        console.error("Error fetching /api/ddns-settings:", e);
    }
}

async function saveDdnsSettings() {
    const status = document.getElementById('ddns-status');
    const payload = {
        cloudflare_ddns_enabled: document.getElementById('ddns-enabled').checked,
        cloudflare_api_token: document.getElementById('ddns-token').value,
        cloudflare_zone_id: document.getElementById('ddns-zone-id').value,
        cloudflare_record_name: document.getElementById('ddns-record-name').value,
        cloudflare_proxied: document.getElementById('ddns-proxied').checked,
        cloudflare_ddns_interval: Number(document.getElementById('ddns-interval').value),
        cloudflare_ttl: Number(document.getElementById('ddns-ttl').value),
        cloudflare_ddns_notify: document.getElementById('ddns-notify').checked
    };
    try {
        const res = await fetch('/api/ddns-settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(await res.text());
        status.innerText = `DDNS 設定已保存。啟用後每 ${payload.cloudflare_ddns_interval} 分鐘檢查一次公網 IP。`;
        return true;
    } catch (e) {
        status.innerText = '保存失敗，請檢查 Cloudflare 資訊。';
        console.error("Error saving /api/ddns-settings:", e);
        return false;
    }
}

function renderDdnsState(data) {
    const rows = [
        ['DNS 記錄 IP', data.last_ip || '尚未同步'],
        ['上次同步', data.last_sync || '—'],
        ['上次檢查', data.last_check || '—'],
    ];
    const box = document.getElementById('ddns-state');
    box.replaceChildren(...rows.map(([label, value]) => {
        const p = document.createElement('p');
        p.textContent = `${label}：${value}`;
        return p;
    }));
    if (data.last_error) {
        const err = document.createElement('p');
        err.className = 'text-red-300 whitespace-normal break-words';
        err.textContent = `最近錯誤：${data.last_error}`;
        box.appendChild(err);
    }
}

function syncDdnsTtlState() {
    const proxied = document.getElementById('ddns-proxied').checked;
    const ttl = document.getElementById('ddns-ttl');
    ttl.disabled = proxied;
    ttl.classList.toggle('opacity-50', proxied);
}
document.getElementById('ddns-proxied').addEventListener('change', syncDdnsTtlState);

async function syncDdnsNow() {
    const status = document.getElementById('ddns-status');
    const saved = await saveDdnsSettings();
    if (!saved) return;
    status.innerText = '正在同步 Cloudflare DNS...';
    try {
        const res = await fetch('/api/ddns-sync', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
        status.innerText = data.action === 'unchanged'
            ? `${data.record} 已是 ${data.ip}，無需更新`
            : `已同步 ${data.record} -> ${data.ip}`;
        fetchDdnsSettings();
    } catch (e) {
        status.innerText = `同步失敗：${e.message}`;
        console.error("Error syncing /api/ddns-sync:", e);
        fetchDdnsSettings();
    }
}

async function fetchBandwagonSettings() {
    try {
        const res = await fetch('/api/bandwagon-settings');
        const data = await res.json();
        document.getElementById('migrate-veid').value = data.bandwagon_veid || '';
        document.getElementById('migrate-api-key').value = data.bandwagon_api_key || '';
    } catch (e) {
        console.error("Error fetching /api/bandwagon-settings:", e);
    }
}

async function saveBandwagonSettings() {
    const payload = {
        bandwagon_veid: document.getElementById('migrate-veid').value,
        bandwagon_api_key: document.getElementById('migrate-api-key').value
    };
    const res = await fetch('/api/bandwagon-settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(await res.text());
    document.getElementById('bandwagon-veid').value = payload.bandwagon_veid;
    document.getElementById('bandwagon-api-key').value = payload.bandwagon_api_key;
}

async function loadBandwagonLocations() {
    const status = document.getElementById('migrate-status');
    const select = document.getElementById('migrate-location');
    try {
        await saveBandwagonSettings();
        status.innerText = '正在讀取可用機房...';
        const res = await fetch('/api/bandwagon/locations');
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
        select.innerHTML = '';
        data.locations.forEach((item) => {
            const option = document.createElement('option');
            option.value = item.code;
            const codeSuffix = item.name.includes(`[${item.code}]`) ? '' : ` [${item.code}]`;
            option.textContent = `${item.name}${codeSuffix}${item.available ? '' : ' / 不可用'}`;
            option.disabled = !item.available;
            select.appendChild(option);
        });
        const current = data.current_location
            ? `<span class="inline-flex mt-2 rounded border border-emerald-400/60 bg-emerald-400/10 px-2 py-1 font-mono text-emerald-200">${escapeHtml(data.current_location)}</span>`
            : `<span class="inline-flex mt-2 rounded border border-amber-400/60 bg-amber-400/10 px-2 py-1 text-amber-200">未能讀取</span>`;
        status.innerHTML = `已讀取 ${data.locations.length} 個機房。現在使用中的機房：<br>${current}`;
    } catch (e) {
        status.innerText = `讀取失敗：${e.message}`;
        console.error("Error fetching /api/bandwagon/locations:", e);
    }
}

let migrateStatusTimer = null;

function renderMigrationStatus(data) {
    const status = document.getElementById('migrate-status');
    const rows = [
        `切換監控中：${data.active ? '是' : '否'}`,
        `舊 IP：${data.old_ip || '未知'}`,
        `目前 IP：${data.current_ip || '未知'}`,
        `新 IP：${data.new_ip || (data.ip_changed ? data.current_ip : '等待中')}`,
    ];
    if (data.ddns?.enabled) {
        rows.push(`DDNS：${data.ddns.synced ? '已同步' : data.ddns.error ? `同步失敗：${data.ddns.error}` : '等待同步'}`);
        rows.push(`域名解析：${data.domain_ok ? (data.ddns.proxied ? 'Cloudflare 代理已同步' : '已解析到新 IP') : `等待解析${data.domain_ips?.length ? `（目前 ${data.domain_ips.join(', ')}）` : ''}`}`);
    }
    const toneClass = data.domain_ok || (data.ip_changed && !data.ddns?.enabled)
        ? 'border-emerald-400/60 bg-emerald-400/10 text-emerald-200'
        : data.ip_changed
            ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200'
            : 'border-amber-400/60 bg-amber-400/10 text-amber-200';
    status.innerHTML = [
        `<span class="inline-flex rounded border px-2 py-1 ${toneClass}">${data.ip_changed ? '已檢測到新 IP' : '正在等待服務器恢復 / IP 變更'}</span>`,
        `<div class="mt-2 space-y-1 font-mono text-[11px] text-slate-300">${rows.map((row) => `<p>${escapeHtml(row)}</p>`).join('')}</div>`
    ].join('');
    if (!data.active && (data.ip_changed || data.domain_ok) && migrateStatusTimer) {
        clearInterval(migrateStatusTimer);
        migrateStatusTimer = null;
        fetchSystem();
        fetchSummary();
    }
}

async function pollMigrationStatus() {
    try {
        const res = await fetch(`/api/bandwagon/migrate-status?t=${Date.now()}`, { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
        renderMigrationStatus(data);
    } catch (e) {
        document.getElementById('migrate-status').innerHTML = '<span class="inline-flex rounded border border-amber-400/60 bg-amber-400/10 px-2 py-1 text-amber-200">切換中，面板暫時無法連接，20 秒後繼續檢測...</span>';
        console.error("Error fetching /api/bandwagon/migrate-status:", e);
    }
}

function startMigrationPolling() {
    if (migrateStatusTimer) clearInterval(migrateStatusTimer);
    pollMigrationStatus();
    migrateStatusTimer = setInterval(pollMigrationStatus, 20000);
}

async function resumeMigrationPollingIfNeeded() {
    try {
        const res = await fetch(`/api/bandwagon/migrate-status?t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (data.active) startMigrationPolling();
    } catch (e) {
        console.error("Error checking migration status:", e);
    }
}

async function startBandwagonMigration() {
    const status = document.getElementById('migrate-status');
    const location = document.getElementById('migrate-location').value;
    const selectedLocationLabel = document.getElementById('migrate-location').selectedOptions[0]?.textContent || location;
    if (!location) {
        status.innerText = '請先讀取並選擇機房。';
        return;
    }
    if (!confirm(`確定要切換到 ${location} 嗎？機房切換通常會造成短暫中斷。`)) return;
    try {
        await saveBandwagonSettings();
        const res = await fetch('/api/bandwagon/migrate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ location, location_label: selectedLocationLabel })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
        status.innerHTML = '<span class="inline-flex rounded border border-cyan-400/60 bg-cyan-400/10 px-2 py-1 text-cyan-200">切換請求已送出，正在檢測新 IP、DDNS 與域名解析...</span>';
        startMigrationPolling();
    } catch (e) {
        status.innerText = `切換失敗：${e.message}`;
        console.error("Error posting /api/bandwagon/migrate:", e);
    }
}

function logoutPanel() {
    fetch('/api/logout', { method: 'POST' }).finally(() => {
        window.location.href = '/login';
    });
}

async function saveSettings(mode) {
    const status = document.getElementById(mode === 'auth' ? 'auth-settings-status' : 'panel-settings-status');
    const payload = {
        auth_username: document.getElementById('settings-username').value,
        auth_password: mode === 'auth' ? document.getElementById('settings-password').value : '',
        panel_title: document.getElementById('settings-title').value,
        panel_subtitle: document.getElementById('settings-subtitle').value
    };

    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(await res.text());
        const saved = await res.json();
        const savedTitle = saved.panel_title || payload.panel_title;
        const savedSubtitle = saved.panel_subtitle || payload.panel_subtitle;

        document.getElementById('settings-password').value = '';
        document.getElementById('settings-title').value = savedTitle;
        document.getElementById('settings-subtitle').value = savedSubtitle;
        document.getElementById('panel-title').innerText = savedTitle;
        document.getElementById('panel-subtitle').innerText = savedSubtitle;
        document.title = savedTitle;
        status.innerText = mode === 'auth' ? '已保存。其他裝置的登入已失效，這個瀏覽器保持登入。' : '已保存，面板文字已更新。';
        closeModal(mode === 'auth' ? 'auth-modal' : 'panel-modal');
    } catch (e) {
        status.innerText = mode === 'auth'
            ? '保存失敗：用戶名 / 密碼只能包含字母、數字與 @ _ - . ! %。'
            : '保存失敗，標題與副標題不能為空。';
        console.error("Error saving /api/settings:", e);
    }
}

async function fetchSystem() {
    try {
        const res = await fetch('/api/system');
        const data = await res.json();
        document.getElementById('sys-ip').innerText = data.public_ip;
        document.getElementById('client-ip').innerText = data.client_ip || 'Unknown';
        document.getElementById('client-location').innerText = data.client_ip_location || 'Unknown';
        document.getElementById('client-coordinates').innerText = formatCoordinates(data.client_ip_lat, data.client_ip_lon);
        document.getElementById('client-timezone').innerText = data.client_ip_timezone || 'Unknown';
        document.getElementById('client-access-type').innerText = data.client_ip_access_type || 'Unknown';
        document.getElementById('ip-location').innerText = data.ip_location || 'Unknown';
        document.getElementById('ip-type').innerText = data.ip_type || '未知';
        renderBandwagonPlan(data.bandwagon);
        document.getElementById('ip-isp').innerText = data.ip_isp || 'Unknown';
        document.getElementById('ip-as').innerText = data.ip_as || 'Unknown';
        document.getElementById('sys-uptime').innerText = data.uptime.split('.')[0];
        document.getElementById('sys-os').innerText = data.os_version;
        document.getElementById('sys-kernel').innerText = data.kernel_release;
        document.getElementById('sys-os-mobile').innerText = data.os_version;
        document.getElementById('sys-kernel-mobile').innerText = data.kernel_release;
        document.getElementById('sys-cpu').innerText = `${data.cpu_percent}%`;
        document.getElementById('sys-cpu-cores').innerText = `${data.cpu_count} 核心`;
        renderLoadAverage(data.load_avg, data.cpu_count);
        document.getElementById('sys-mem').innerText = `${data.memory_percent}%`;
        document.getElementById('sys-mem-detail').innerText = `${(data.memory_used/1073741824).toFixed(1)} / ${(data.memory_total/1073741824).toFixed(1)} GB`;
        document.getElementById('sys-disk').innerText = `${data.disk_percent}%`;
        document.getElementById('sys-disk-detail').innerText = `${(data.disk_used/1073741824).toFixed(1)} / ${(data.disk_total/1073741824).toFixed(1)} GB`;
        document.getElementById('footer-iface').innerText = data.interface || 'auto';
        setProgress('sys-cpu-bar', data.cpu_percent);
        setProgress('sys-mem-bar', data.memory_percent);
        setProgress('sys-disk-bar', data.disk_percent);
        
        const dot = document.getElementById('iface-status-dot');
        if (data.interface_status) {
            dot.className = 'status-dot-ok';
        } else {
            dot.className = 'status-dot-error';
            document.getElementById('footer-iface').innerText = data.interface + '（已斷線）';
        }
    } catch (e) {
        console.error("Error fetching /api/system:", e);
    }
}

async function fetchRealtime() {
    try {
        const res = await fetch('/api/realtime');
        const data = await res.json();
        setSpeed('rx-speed', data.rx_speed);
        setSpeed('tx-speed', data.tx_speed);
    } catch (e) {
        console.error("Error fetching /api/realtime:", e);
    }
}

async function fetchLatency() {
    const target = document.getElementById('latency-ms');
    const startedAt = performance.now();
    try {
        const res = await fetch(`/api/ping?t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(await res.text());
        const latency = Math.round(performance.now() - startedAt);
        target.innerText = `${latency} ms`;
        target.className = latency < 120 ? 'font-mono text-emerald-300' : latency < 260 ? 'font-mono text-amber-300' : 'font-mono text-red-300';
    } catch (e) {
        target.innerText = '失敗';
        target.className = 'font-mono text-red-300';
        console.error("Error fetching /api/ping:", e);
    }
}

async function fetchSummary() {
    try {
        const res = await fetch('/api/summary');
        const data = await res.json();
        document.getElementById('today-rx').innerText = formatBytes(data.today_rx);
        document.getElementById('today-tx').innerText = formatBytes(data.today_tx);
        document.getElementById('month-rx').innerText = formatBytes(data.month_rx);
        document.getElementById('month-tx').innerText = formatBytes(data.month_tx);
        document.getElementById('total-rx').innerText = formatBytes(data.total_rx);
        document.getElementById('total-tx').innerText = formatBytes(data.total_tx);
        document.getElementById('total-since').innerText = data.total_since || '未知';
        document.getElementById('reset-day').innerText = `每月 ${data.month_reset_day} 日重置`;
        document.getElementById('reset-day-select').value = String(data.month_reset_day);
        renderTrafficQuota(data.traffic_quota);
        renderVpsExpiry(data.vps_expiry);
    } catch (e) {
        console.error("Error fetching /api/summary:", e);
    }
}

async function resetTraffic() {
    if (!confirm('確定要重置所有流量統計嗎？這會清空目前累積的流量資料。')) return;
    try {
        const res = await fetch('/api/traffic/reset', { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        await fetchSummary();
        await fetchCharts();
    } catch (e) {
        alert('重置失敗，請稍後再試。');
        console.error("Error resetting traffic:", e);
    }
}

async function saveResetDay() {
    const status = document.getElementById('reset-day-status');
    const day = Number(document.getElementById('reset-day-select').value);
    try {
        const res = await fetch('/api/reset-day', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({month_reset_day: day})
        });
        if (!res.ok) throw new Error(await res.text());
        status.innerText = '已保存，本期流量已按新日期重新統計。';
        closeModal('reset-day-modal');
        await fetchSummary();
    } catch (e) {
        status.innerText = '保存失敗，請選擇 1-31 之間的日期。';
        console.error("Error saving reset day:", e);
    }
}

async function fetchTrafficQuotaSettings() {
    try {
        const res = await fetch('/api/traffic-quota');
        const data = await res.json();
        const input = bytesToQuotaInput(data.traffic_quota_bytes);
        document.getElementById('traffic-quota-value').value = input.value;
        document.getElementById('traffic-quota-unit').value = input.unit;
        document.getElementById('traffic-quota-api-status').innerText = data.bandwagon_available
            ? '已檢測到 Bandwagon 官方 API，面板會優先使用官方配額。'
            : `未檢測到 Bandwagon 官方配額，將使用手動配額。${data.bandwagon_error ? `原因：${data.bandwagon_error}` : ''}`;
        document.getElementById('traffic-alert-enabled').checked = data.traffic_alert_enabled !== false;
        document.getElementById('vps-expiry-date').value = data.vps_expiry_date || '';
        document.getElementById('vps-expiry-notify').checked = data.vps_expiry_notify !== false;
        document.getElementById('traffic-alert-thresholds').value = data.traffic_alert_thresholds || '80,90,100';
        document.getElementById('traffic-alert-hint').innerText = data.telegram_ready
            ? '每個門檻在一個計費週期內只提醒一次，重置後重新計算。'
            : '尚未設定 Telegram Bot Token / Chat ID，請先到「TG 推送」填寫，否則不會收到提醒。';
    } catch (e) {
        document.getElementById('traffic-quota-api-status').innerText = '讀取配額設定失敗。';
        console.error("Error fetching /api/traffic-quota:", e);
    }
}

async function saveTrafficQuotaSettings() {
    const status = document.getElementById('traffic-quota-status');
    const quotaBytes = Math.max(0, quotaInputToBytes() || 0);
    try {
        const res = await fetch('/api/traffic-quota', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                traffic_quota_bytes: quotaBytes,
                traffic_alert_enabled: document.getElementById('traffic-alert-enabled').checked,
                traffic_alert_thresholds: document.getElementById('traffic-alert-thresholds').value,
                vps_expiry_date: document.getElementById('vps-expiry-date').value,
                vps_expiry_notify: document.getElementById('vps-expiry-notify').checked
            })
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const data = await res.json();
        renderTrafficQuota(data.traffic_quota);
        renderVpsExpiry(data.vps_expiry);
        status.innerText = '已保存。';
        await fetchTrafficQuotaSettings();
        await fetchSummary();
        closeModal('traffic-quota-modal');
    } catch (e) {
        status.innerText = `保存失敗：${e.message.includes('threshold') ? '提醒門檻請填 1–200 之間的數字，用逗號分隔' : e.message.includes('expiry') ? '到期日格式不正確' : '請檢查流量配額'}`;
        console.error("Error saving /api/traffic-quota:", e);
    }
}

const chartOpts = {
    backgroundColor: 'transparent',
    tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params) => {
            if (!params || params.length === 0) return '';
            const rows = [params[0].axisValueLabel || params[0].axisValue || ''];
            params.forEach((item) => rows.push(`${item.marker}${item.seriesName}: ${formatBytes(item.value)}`));
            return rows.join('<br>');
        }
    },
    legend: { data: ['下載', '上傳'], bottom: 0, textStyle: { color: '#94a3b8' } },
    grid: { left: '2%', right: '4%', bottom: '15%', top: '15%', containLabel: true },
    xAxis: { type: 'category', data: [], axisLabel: { color: '#64748b' } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#334155' } }, axisLabel: { color: '#64748b', formatter: (val) => formatBytes(val) } },
    series: [
        { name: '下載', type: 'bar', itemStyle: {color: '#34d399', borderRadius: [4, 4, 0, 0]}, data: [] },
        { name: '上傳', type: 'bar', itemStyle: {color: '#60a5fa', borderRadius: [4, 4, 0, 0]}, data: [] }
    ]
};

let chart24h = echarts.init(document.getElementById('chart-24h'));
let chart30d = echarts.init(document.getElementById('chart-30d'));
let chart12m = echarts.init(document.getElementById('chart-12m'));

chart24h.setOption({...chartOpts, title: { text: '最近 24 小時', textStyle: {color: '#cbd5e1', fontSize: 16} }});
chart30d.setOption({...chartOpts, title: { text: '最近 30 天', textStyle: {color: '#cbd5e1', fontSize: 16} }});
chart12m.setOption({...chartOpts, title: { text: '最近 12 個月', textStyle: {color: '#cbd5e1', fontSize: 16} }});

async function fetchCharts() {
    try {
        const [r1, r2, r3] = await Promise.all([fetch('/api/hourly'), fetch('/api/daily'), fetch('/api/monthly')]);
        const hData = await r1.json();
        const dData = await r2.json();
        const mData = await r3.json();
        
        chart24h.setOption({
            xAxis: { data: hData.map(d => `${d.month}-${d.day} ${d.hour}:00`) },
            series: [{ data: hData.map(d => d.rx_bytes) }, { data: hData.map(d => d.tx_bytes) }]
        });
        chart30d.setOption({
            xAxis: { data: dData.map(d => `${d.month}-${d.day}`) },
            series: [{ data: dData.map(d => d.rx_bytes) }, { data: dData.map(d => d.tx_bytes) }]
        });
        chart12m.setOption({
            xAxis: { data: mData.map(d => `${d.year}-${d.month}`) },
            series: [{ data: mData.map(d => d.rx_bytes) }, { data: mData.map(d => d.tx_bytes) }]
        });
    } catch (e) {
        console.error("Error fetching charts:", e);
    }
}

buildResetDayOptions();
buildTelegramScheduleOptions();
document.getElementById('open-auth-settings').addEventListener('click', () => openModal('auth-modal'));
document.getElementById('open-panel-settings').addEventListener('click', () => openModal('panel-modal'));
document.getElementById('open-telegram-settings').addEventListener('click', () => openModal('telegram-modal'));
document.getElementById('open-ddns-settings').addEventListener('click', () => openModal('ddns-modal'));
document.getElementById('open-migrate-settings').addEventListener('click', () => openModal('migrate-modal'));
document.getElementById('open-traffic-quota').addEventListener('click', () => openModal('traffic-quota-modal'));
document.getElementById('open-reset-day').addEventListener('click', () => openModal('reset-day-modal'));
document.getElementById('ssl-summary').addEventListener('click', () => openModal('ssl-modal'));
document.getElementById('ssl-refresh').addEventListener('click', async () => {
    const note = document.getElementById('ssl-status-note');
    note.textContent = '正在重新檢查...';
    await fetchSslStatus(true);
    note.textContent = '已重新檢查。面板直接連線自己的 HTTPS 端口，讀出瀏覽器實際看到的證書';
});
document.getElementById('ssl-expiry-notify').addEventListener('change', saveSslNotify);
document.getElementById('ssl-scan').addEventListener('click', sslScan);
document.getElementById('ssl-apply').addEventListener('click', sslApply);
document.getElementById('auth-settings-save').addEventListener('click', () => saveSettings('auth'));
document.getElementById('panel-settings-save').addEventListener('click', () => saveSettings('panel'));
document.getElementById('telegram-save').addEventListener('click', saveTelegramSettings);
document.getElementById('telegram-test').addEventListener('click', testTelegramPush);
document.getElementById('ddns-save').addEventListener('click', saveDdnsSettings);
document.getElementById('ddns-sync').addEventListener('click', syncDdnsNow);
document.getElementById('migrate-load').addEventListener('click', loadBandwagonLocations);
document.getElementById('migrate-start').addEventListener('click', startBandwagonMigration);
document.getElementById('vps-restart').addEventListener('click', restartVps);
document.getElementById('open-iface').addEventListener('click', openInterfacePicker);
document.getElementById('iface-save').addEventListener('click', saveInterface);
document.getElementById('speed-toggle').addEventListener('click', toggleSpeedUnit);
document.getElementById('traffic-quota-save').addEventListener('click', saveTrafficQuotaSettings);
document.getElementById('logout-panel').addEventListener('click', logoutPanel);
document.getElementById('traffic-reset').addEventListener('click', resetTraffic);
document.getElementById('reset-day-save').addEventListener('click', saveResetDay);
document.querySelectorAll('.settings-close').forEach((button) => {
    button.addEventListener('click', () => closeModal(button.dataset.modal));
});
fetchSystem(); fetchSummary(); fetchCharts(); fetchSettings(); fetchTelegramSettings(); fetchDdnsSettings(); fetchBandwagonSettings(); fetchTrafficQuotaSettings(); fetchLatency(); fetchSslStatus(); resumeMigrationPollingIfNeeded();
setInterval(fetchRealtime, 2000);
setInterval(fetchLatency, 10000);
setInterval(fetchSummary, 5000);
setInterval(fetchSystem, 5000);
setInterval(fetchCharts, 60000);
setInterval(fetchSslStatus, 600000);
window.addEventListener('resize', () => { chart24h.resize(); chart30d.resize(); chart12m.resize(); });
