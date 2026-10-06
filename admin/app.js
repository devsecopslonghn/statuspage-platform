const API_PREFIX = '/api';
let csrfToken = '';
let groupsData = [];
let componentsData = [];

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => { toast.remove(); }, 3000);
}

async function apiCall(path, method = 'GET', body = null) {
    const headers = {};
    if (method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        headers['X-CSRF-Token'] = csrfToken;
    }
    const options = { method, headers };
    if (body) options.body = JSON.stringify(body);
    
    const res = await fetch(API_PREFIX + path, options);
    const contentType = res.headers.get('content-type');
    let data = {};
    if (contentType && contentType.includes('application/json')) {
        data = await res.json();
    }
    if (res.redirected || !contentType || !contentType.includes('application/json')) {
        throw new Error('Phiên đăng nhập đã hết hạn. Hãy tải lại trang.');
    }
    if (!res.ok) {
        throw new Error(data.error || `HTTP error ${res.status}`);
    }
    return data;
}

async function initSession() {
    try {
        const data = await apiCall('/session');
        csrfToken = data.csrf_token;
        document.getElementById('user-info').textContent = data.user;
        await loadData();
        await loadAudit();
    } catch (e) {
        showToast('Không thể khởi tạo phiên làm việc: ' + e.message, 'error');
    }
}


function observationTime(value) {
    return value ? new Date(value * 1000).toLocaleString('vi-VN') : 'Chưa có quan sát';
}

async function loadInventory() {
    const summary = document.getElementById('inventory-summary');
    const tbody = document.querySelector('#inventory-table tbody');
    try {
        const inventory = await apiCall('/inventory');
        const sources = Object.entries(inventory.sources || {}).map(([kind, source]) =>
            `${kind}: ${source.ok ? 'đọc thành công' : 'mất nguồn'} (${observationTime(source.observed_at)})`);
        summary.textContent = `${inventory.available ? sources.join('; ') : 'Collector không có dữ liệu mới'} · Giới hạn component: ${inventory.component_limit || 'chưa xác minh; chặn tạo mới'}`;
        tbody.textContent = '';
        (inventory.rows || []).forEach(row => {
            const observation = row.observation || {};
            const tr = document.createElement('tr');
            const conditions = Object.entries(row.conditions || {}).map(([kind, value]) => `${kind}=${value.status}`).join(', ');
            const values = [
                `${row.name || row.key} (${row.key})`,
                [row.url || '', row.logo_url ? `Logo: ${row.logo_url}` : '', ...(row.aliases || []).map(url => `Alias: ${url}`), ...(row.secondary_endpoints || []).map(url => `Phụ: ${url}`)].join('\n'),
                `${row.source || 'Chưa có nguồn'}; nguồn ${observationTime(row.source_observed_at)}; thấy cuối ${observationTime(row.last_seen_at)}`,
                `${row.check_type || ''}; ${row.assurance || ''}; HTTP expected ${JSON.stringify(row.expected_status || [])}; redirect ${row.redirect || ''}`,
                `${observation.status || 'unknown'}: ${observation.reason || 'Chưa có quan sát'}; ${observationTime(observation.observed_at)}; HTTP ${observation.http_status ?? '-'}; ${conditions}${observation.cordoned ? '; cordoned' : ''}`,
                `${row.publication}; ${row.component_id || 'chưa gắn ID'}${row.sync ? `; sync ${row.sync.stale ? 'stale' : row.sync.final_status || 'đang debounce'}; public ${row.sync.statuspage_status}; ${row.sync.write_error || row.sync.reason}; ${observationTime(row.sync.observed_at)}` : ''}`
            ];
            values.forEach((value, index) => {
                const td = document.createElement('td');
                if (index === 1) {
                    value.split('\n').filter(Boolean).forEach(line => {
                        const url = line.replace(/^(Alias|Phụ|Logo): /, '');
                        try {
                            const target = new URL(url);
                            if (target.protocol !== 'https:' || target.username || target.password) throw new Error('Unsafe URL');
                            const link = document.createElement('a');
                            link.href = target.href;
                            link.target = '_blank';
                            link.rel = 'noopener noreferrer';
                            link.textContent = line;
                            td.appendChild(link);
                        } catch (_) { td.appendChild(document.createTextNode(line)); }
                        td.appendChild(document.createElement('br'));
                    });
                } else { td.textContent = value; }
                td.style.whiteSpace = 'pre-line';
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
    } catch (error) {
        tbody.textContent = '';
        summary.textContent = 'Không thể đọc inventory; dữ liệu hiện tại không xác định.';
    }
}

async function loadData() {
    await loadInventory();
    try {
        const data = await apiCall('/overview');
        // Update Page Form
        document.getElementById('page-name').value = data.page.name || '';
        document.getElementById('page-url').value = data.page.url || '';
        document.getElementById('page-headline').value = data.page.headline || '';
        document.getElementById('page-description').value = data.page.page_description || '';
        
        // Update Incidents
        const incList = document.getElementById('incidents-list');
        incList.textContent = '';
        if (!data.incidents || data.incidents.length === 0) {
            incList.textContent = 'Không có sự cố nào.';
        } else {
            const ul = document.createElement('ul');
            data.incidents.forEach(inc => {
                const li = document.createElement('li');
                li.textContent = `${inc.name} - Trạng thái: ${inc.status} `;
                if (inc.shortlink && inc.shortlink.startsWith('https://')) {
                    const a = document.createElement('a');
                    a.href = inc.shortlink;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    a.textContent = '[Link]';
                    li.appendChild(a);
                }
                ul.appendChild(li);
            });
            incList.appendChild(ul);
        }

        // Update Monitor Config
        const monTbody = document.querySelector('#monitor-table tbody');
        monTbody.textContent = '';
        if (!data.monitor_config || data.monitor_config.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 3;
            td.textContent = 'Không có dữ liệu config.';
            tr.appendChild(td);
            monTbody.appendChild(tr);
        } else {
            data.monitor_config.forEach(m => {
                const tr = document.createElement('tr');
                const t1 = document.createElement('td'); t1.textContent = m.key;
                const t2 = document.createElement('td'); t2.textContent = m.mode;
                const t3 = document.createElement('td'); t3.textContent = m.check_count;
                tr.appendChild(t1); tr.appendChild(t2); tr.appendChild(t3);
                monTbody.appendChild(tr);
            });
        }

        groupsData = data.groups || [];
        componentsData = data.components || [];

        // Update group select
        const gSelect = document.getElementById('comp-group');
        gSelect.textContent = '';
        const noneOption = document.createElement('option');
        noneOption.value = ''; noneOption.textContent = '(Không có)';
        gSelect.appendChild(noneOption);
        groupsData.forEach(g => {
            const opt = document.createElement('option');
            opt.value = g.id;
            opt.textContent = g.name;
            gSelect.appendChild(opt);
        });

        // Update Components Table
        const cTbody = document.querySelector('#components-table tbody');
        cTbody.textContent = '';
        const allItems = [...groupsData.map(g => ({...g, isGroup: true})), ...componentsData.map(c => ({...c, isGroup: false}))];
        
        allItems.forEach(item => {
            const tr = document.createElement('tr');
            const tName = document.createElement('td');
            tName.textContent = (item.isGroup ? '[Nhóm] ' : '') + item.name;
            const tDesc = document.createElement('td');
            tDesc.textContent = item.description || '';
            const tGroup = document.createElement('td');
            if (!item.isGroup && item.group_id) {
                const g = groupsData.find(g => g.id === item.group_id);
                tGroup.textContent = g ? g.name : item.group_id;
            }
            const tStatus = document.createElement('td');
            tStatus.textContent = item.status || '';
            const tShowcase = document.createElement('td');
            tShowcase.textContent = item.isGroup ? '' : (item.showcase ? 'Có' : 'Không');
            
            const tAction = document.createElement('td');
            const btn = document.createElement('button');
            btn.className = 'btn btn-secondary';
            btn.textContent = 'Sửa';
            btn.onclick = () => openModal(item);
            tAction.appendChild(btn);

            tr.appendChild(tName);
            tr.appendChild(tDesc);
            tr.appendChild(tGroup);
            tr.appendChild(tStatus);
            tr.appendChild(tShowcase);
            tr.appendChild(tAction);
            cTbody.appendChild(tr);
        });
        
    } catch (e) {
        showToast('Lỗi tải dữ liệu: ' + e.message, 'error');
    }
}

async function loadAudit() {
    try {
        const audits = await apiCall('/audit');
        const aTbody = document.querySelector('#audit-table tbody');
        aTbody.textContent = '';
        if (!audits || audits.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 6;
            td.textContent = 'Chưa có lịch sử.';
            tr.appendChild(td);
            aTbody.appendChild(tr);
        } else {
            audits.forEach(a => {
                const tr = document.createElement('tr');
                const fields = ['timestamp', 'user', 'operation', 'target'];
                fields.forEach(f => {
                    const td = document.createElement('td');
                    td.textContent = a[f];
                    tr.appendChild(td);
                });
                const tChg = document.createElement('td');
                tChg.textContent = JSON.stringify(a.changed_fields);
                const tRes = document.createElement('td');
                tRes.textContent = a.result;
                tr.appendChild(tChg);
                tr.appendChild(tRes);
                aTbody.appendChild(tr);
            });
        }
    } catch(e) {
        console.error('Audit fetch error', e);
    }
}

document.getElementById('page-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!confirm('Bạn có chắc muốn lưu thay đổi trang?')) return;
    
    const btn = document.getElementById('btn-save-page');
    btn.disabled = true;
    btn.textContent = 'Đang lưu...';
    try {
        const body = {
            name: document.getElementById('page-name').value,
            url: document.getElementById('page-url').value,
            headline: document.getElementById('page-headline').value,
            page_description: document.getElementById('page-description').value
        };
        await apiCall('/pages/hsyg2r4ql1pr', 'PATCH', body);
        showToast('Đã lưu cấu hình trang');
        await loadData();
        await loadAudit();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Lưu cấu hình trang';
    }
});

const modal = document.getElementById('component-modal');
function openModal(item) {
    document.getElementById('comp-id').value = item.id;
    document.getElementById('comp-is-group').value = item.isGroup ? 'true' : 'false';
    document.getElementById('comp-name').value = item.name || '';
    document.getElementById('comp-desc').value = item.description || '';
    
    if (item.isGroup) {
        document.getElementById('group-select-container').style.display = 'none';
        document.getElementById('showcase-container').style.display = 'none';
    } else {
        document.getElementById('group-select-container').style.display = 'block';
        document.getElementById('comp-group').value = item.group_id || '';
        document.getElementById('showcase-container').style.display = 'flex';
        document.getElementById('comp-showcase').checked = !!item.showcase;
    }
    
    document.getElementById('comp-desc').disabled = !!item.isGroup;
    document.getElementById('modal-title').textContent = item.isGroup ? 'Đổi tên nhóm' : 'Sửa thành phần';
    modal.classList.add('active');
    modal.showModal();
    document.getElementById('comp-name').focus();
}

document.getElementById('btn-cancel-modal').onclick = () => {
    modal.classList.remove('active');
    modal.close();
};

document.getElementById('component-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!confirm('Bạn có chắc muốn lưu thay đổi này?')) return;
    
    const btn = document.getElementById('btn-save-comp');
    btn.disabled = true;
    btn.textContent = 'Đang lưu...';
    
    try {
        const id = document.getElementById('comp-id').value;
        const isGroup = document.getElementById('comp-is-group').value === 'true';
        const body = {
            name: document.getElementById('comp-name').value,
            description: document.getElementById('comp-desc').value
        };
        if (!isGroup) {
            const gid = document.getElementById('comp-group').value;
            if (gid) body.group_id = gid;
            else body.group_id = null;
            body.showcase = document.getElementById('comp-showcase').checked;
        }
        
        if (isGroup) delete body.description;
        await apiCall((isGroup ? '/groups/' : '/components/') + id, 'PATCH', body);
        showToast('Đã cập nhật');
        modal.classList.remove('active');
    modal.close();
        await loadData();
        await loadAudit();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Xác nhận Lưu';
    }
});

document.getElementById('btn-refresh').addEventListener('click', async () => {
    const btn = document.getElementById('btn-refresh'); btn.disabled = true;
    try { await loadData(); await loadAudit(); } finally { btn.disabled = false; }
});
modal.addEventListener('cancel', () => modal.classList.remove('active'));
initSession();
