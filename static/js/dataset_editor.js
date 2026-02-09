(function () {
    const statusEl = document.getElementById('editor-status');
    const saveBtn = document.getElementById('save-btn');
    const backBtn = document.getElementById('back-btn');
    const addRowBtn = document.getElementById('add-row-btn');
    const prevBtn = document.getElementById('page-prev');
    const nextBtn = document.getElementById('page-next');
    const rangeEl = document.getElementById('page-range');
    const headEl = document.getElementById('editor-head');
    const bodyEl = document.getElementById('editor-body');

    const state = {
        token: null,
        columns: [],
        rows: [],
        page: 1,
        pageSize: 50,
        dirty: false,
    };

    function setText(el, value) {
        if (!el) return;
        el.textContent = value == null ? '' : String(value);
    }

    function setLoading(button, loading, label) {
        if (!button) return;
        if (loading) {
            button.disabled = true;
            if (!button.dataset.originalLabel) button.dataset.originalLabel = button.textContent.trim();
            button.textContent = label || '...';
            button.classList.add('is-loading');
        } else {
            button.classList.remove('is-loading');
            button.disabled = false;
            if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
        }
    }

    function randomId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return `row-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function markDirty() {
        state.dirty = true;
    }

    function updateControls() {
        const hasData = state.columns.length > 0;
        if (addRowBtn) addRowBtn.disabled = !hasData;
        if (saveBtn) saveBtn.disabled = !hasData;
        const totalPages = Math.max(1, Math.ceil(state.rows.length / state.pageSize));
        state.page = Math.min(Math.max(1, state.page), totalPages);
        if (prevBtn) prevBtn.disabled = state.page <= 1;
        if (nextBtn) nextBtn.disabled = state.page >= totalPages;
        const start = state.rows.length ? (state.page - 1) * state.pageSize + 1 : 0;
        const end = Math.min(state.rows.length, state.page * state.pageSize);
        setText(rangeEl, state.rows.length ? `${start}-${end} / ${state.rows.length}` : '—');
    }

    function getVisibleRows() {
        const start = (state.page - 1) * state.pageSize;
        return state.rows.slice(start, start + state.pageSize);
    }

    function render() {
        if (!headEl || !bodyEl) return;
        headEl.innerHTML = '';
        state.columns.forEach((col) => {
            const th = document.createElement('th');
            th.textContent = col;
            headEl.appendChild(th);
        });
        const actionsTh = document.createElement('th');
        actionsTh.textContent = 'Actions';
        headEl.appendChild(actionsTh);

        bodyEl.innerHTML = '';
        getVisibleRows().forEach((row) => {
            const tr = document.createElement('tr');
            tr.dataset.rowId = row.row_id;
            state.columns.forEach((col) => {
                const td = document.createElement('td');
                const input = document.createElement('input');
                input.type = 'text';
                input.value = row.values && row.values[col] != null ? String(row.values[col]) : '';
                input.dataset.rowId = row.row_id;
                input.dataset.column = col;
                input.className = 'table-input';
                input.addEventListener('input', (event) => {
                    const rowId = event.target.dataset.rowId;
                    const column = event.target.dataset.column;
                    const targetRow = state.rows.find((r) => r.row_id === rowId);
                    if (!targetRow) return;
                    targetRow.values[column] = event.target.value;
                    markDirty();
                });
                td.appendChild(input);
                tr.appendChild(td);
            });
            const actionTd = document.createElement('td');
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'btn btn-danger btn-compact';
            delBtn.textContent = 'Supprimer';
            delBtn.addEventListener('click', () => {
                state.rows = state.rows.filter((r) => r.row_id !== row.row_id);
                markDirty();
                updateControls();
                render();
            });
            actionTd.appendChild(delBtn);
            tr.appendChild(actionTd);
            bodyEl.appendChild(tr);
        });
        updateControls();
    }

    async function loadState() {
        const response = await fetch('/predict/batch/state?include_rows=1', {
            headers: { 'Accept': 'application/json' },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.message || 'Impossible de charger le dataset.');
        }
        return data;
    }

    async function saveAll() {
        if (!state.token) return;
        setLoading(saveBtn, true, 'Enregistrement…');
        try {
            const response = await fetch('/predict/batch/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: state.token, rows: state.rows }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || 'Impossible d’enregistrer.');
            }
            state.dirty = false;
            if (window.showToast) window.showToast({ message: 'Modifications enregistrées.', tone: 'success' });
            window.location.assign('/predict/batch');
        } catch (err) {
            if (window.showToast) window.showToast({ message: err.message || 'Erreur lors de l’enregistrement.', tone: 'error' });
        } finally {
            setLoading(saveBtn, false);
        }
    }

    function goBack() {
        if (state.dirty) {
            const ok = window.confirm('Des modifications non enregistrées seront perdues. Continuer ?');
            if (!ok) return;
        }
        window.location.assign('/predict/batch');
    }

    function bind() {
        if (prevBtn) prevBtn.addEventListener('click', () => { state.page -= 1; render(); });
        if (nextBtn) nextBtn.addEventListener('click', () => { state.page += 1; render(); });
        if (addRowBtn) {
            addRowBtn.addEventListener('click', () => {
                const values = {};
                state.columns.forEach((col) => { values[col] = null; });
                state.rows.push({ row_id: randomId(), values });
                markDirty();
                const totalPages = Math.max(1, Math.ceil(state.rows.length / state.pageSize));
                state.page = totalPages;
                render();
            });
        }
        if (saveBtn) saveBtn.addEventListener('click', saveAll);
        if (backBtn) backBtn.addEventListener('click', goBack);
    }

    async function init() {
        bind();
        try {
            const data = await loadState();
            state.token = data.token;
            state.columns = data.columns || [];
            state.rows = data.rows || [];
            setText(statusEl, `Chargé : ${state.rows.length} lignes · ${state.columns.length} colonnes`);
            render();
        } catch (err) {
            setText(statusEl, err.message || 'Erreur.');
            if (window.showToast) window.showToast({ message: err.message || 'Erreur.', tone: 'error' });
        }
    }

    document.addEventListener('DOMContentLoaded', init);
})();

