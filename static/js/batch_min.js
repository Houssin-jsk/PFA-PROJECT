(function () {
    const fileInput = document.getElementById('batch-file-input');
    const importBtn = document.getElementById('import-btn');
    const predictBtn = document.getElementById('predict-btn');
    const exportMenu = document.getElementById('export-menu');
    const saveBtn = document.getElementById('save-btn');
    const editBtn = document.getElementById('edit-btn');

    const fileNameEl = document.getElementById('file-name');
    const rowsEl = document.getElementById('summary-rows');
    const colsEl = document.getElementById('summary-cols');
    const missingEl = document.getElementById('summary-missing');
    const validationEl = document.getElementById('summary-validation');
    const statusEl = document.getElementById('batch-status');

    const state = {
        token: null,
        hasPredictions: false,
        fileName: null,
    };

    function setText(el, value) {
        if (!el) return;
        el.textContent = value == null || value === '' ? '—' : String(value);
    }

    function setStatus(message, tone = 'info') {
        setText(statusEl, message);
        if (statusEl) statusEl.dataset.tone = tone;
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

    function updateActions() {
        const hasToken = Boolean(state.token);
        if (predictBtn) predictBtn.disabled = !hasToken;
        if (editBtn) editBtn.disabled = !hasToken;
        if (saveBtn) saveBtn.disabled = !state.hasPredictions;
        if (exportMenu) exportMenu.toggleAttribute('data-disabled', !state.hasPredictions);
        if (exportMenu && !state.hasPredictions) exportMenu.removeAttribute('open');
        if (exportMenu) {
            exportMenu.querySelectorAll('button[data-format]').forEach((btn) => {
                btn.disabled = !state.hasPredictions;
            });
        }
        if (fileNameEl) {
            setText(fileNameEl, state.fileName || 'Aucun fichier importé.');
        }
    }

    async function fetchState() {
        const response = await fetch('/predict/batch/state', { headers: { 'Accept': 'application/json' } });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            return null;
        }
        return data;
    }

    function renderSummary(data) {
        const summary = (data && data.summary) || {};
        setText(rowsEl, data && typeof data.row_count === 'number' ? data.row_count : '—');
        setText(colsEl, data && typeof data.column_count === 'number' ? data.column_count : '—');
        setText(missingEl, data && typeof data.missing_cells === 'number' ? data.missing_cells : '—');

        if (!data || !data.token) {
            setText(validationEl, '—');
            setStatus('Importez un fichier pour commencer.');
            return;
        }

        if (data.missing_features && data.missing_features.length) {
            setText(validationEl, 'Colonnes manquantes');
            setStatus('Le dataset ne contient pas toutes les colonnes requises.', 'error');
            return;
        }

        const invalid = Number(summary.invalid || 0);
        const valid = Number(summary.valid || 0);
        const total = Number(summary.total || data.row_count || 0);
        if (summary.ready) {
            setText(validationEl, 'Prêt');
            setStatus(`${valid}/${total} lignes valides. Prêt pour la prédiction.`, 'success');
        } else if (invalid > 0) {
            setText(validationEl, 'À corriger');
            setStatus(`${invalid} lignes invalides. Ouvrez l’éditeur pour corriger.`, 'error');
        } else {
            setText(validationEl, 'À compléter');
            setStatus('Validation incomplète. Vérifiez le dataset.', 'info');
        }
    }

    async function uploadFile(file) {
        const formData = new FormData();
        formData.append('data_file', file);
        setLoading(importBtn, true, 'Import…');
        try {
            const response = await fetch('/predict/batch/upload', { method: 'POST', body: formData });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || 'Import impossible.');
            }
            state.token = data.token;
            state.hasPredictions = false;
            state.fileName = file.name;
            updateActions();
            const stateData = await fetchState();
            renderSummary(stateData);
            if (window.showToast) window.showToast({ message: 'Fichier importé avec succès.', tone: 'success' });
        } catch (err) {
            state.token = null;
            state.hasPredictions = false;
            updateActions();
            renderSummary(null);
            if (window.showToast) window.showToast({ message: err.message || 'Erreur lors de l’import.', tone: 'error' });
        } finally {
            setLoading(importBtn, false);
        }
    }

    async function predictBatch() {
        if (!state.token) return;
        setLoading(predictBtn, true, 'Calcul…');
        try {
            const response = await fetch('/predict/batch/predict', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: state.token }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || 'Prédiction impossible.');
            }
            state.hasPredictions = true;
            updateActions();
            if (window.showToast) window.showToast({ message: 'Prédiction batch terminée.', tone: 'success' });
        } catch (err) {
            state.hasPredictions = false;
            updateActions();
            if (window.showToast) window.showToast({ message: err.message || 'Erreur lors de la prédiction.', tone: 'error' });
        } finally {
            setLoading(predictBtn, false);
        }
    }

    async function exportResults(format) {
        if (!state.token || !state.hasPredictions) return;
        try {
            const response = await fetch('/predict/batch/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: state.token, format }),
            });
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                throw new Error(err.message || 'Export impossible.');
            }
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            const disposition = response.headers.get('Content-Disposition');
            let filename = format === 'excel' ? 'predictions.xlsx' : 'predictions.csv';
            if (disposition) {
                const match = disposition.match(/filename="?([^"]+)"?/i);
                if (match && match[1]) filename = match[1];
            }
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            window.URL.revokeObjectURL(url);
        } catch (err) {
            if (window.showToast) window.showToast({ message: err.message || 'Erreur lors de l’export.', tone: 'error' });
        }
    }

    async function saveToHistory() {
        if (!state.token || !state.hasPredictions) return;
        setLoading(saveBtn, true, 'Enregistrement…');
        try {
            const response = await fetch('/predict/batch/history', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: state.token }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || 'Enregistrement impossible.');
            }
            if (window.showToast) window.showToast({ message: `${Number(data.saved || 0)} clients enregistrés dans l’historique.`, tone: 'success' });
        } catch (err) {
            if (window.showToast) window.showToast({ message: err.message || 'Erreur lors de l’enregistrement.', tone: 'error' });
        } finally {
            setLoading(saveBtn, false);
        }
    }

    function bind() {
        if (importBtn && fileInput) {
            importBtn.addEventListener('click', () => fileInput.click());
            fileInput.addEventListener('change', () => {
                if (!fileInput.files || !fileInput.files.length) return;
                uploadFile(fileInput.files[0]);
            });
        }
        if (predictBtn) predictBtn.addEventListener('click', predictBatch);
        if (saveBtn) saveBtn.addEventListener('click', saveToHistory);
        if (editBtn) editBtn.addEventListener('click', () => window.location.assign('/dataset/editor'));
        if (exportMenu) {
            const exportSummary = exportMenu.querySelector('summary');
            if (exportSummary) {
                exportSummary.addEventListener('click', (event) => {
                    if (exportMenu.hasAttribute('data-disabled')) {
                        event.preventDefault();
                    }
                });
            }
            exportMenu.addEventListener('click', (event) => {
                const btn = event.target.closest('button[data-format]');
                if (!btn) return;
                exportResults(btn.dataset.format);
                exportMenu.removeAttribute('open');
            });
        }
    }

    async function init() {
        updateActions();
        const data = await fetchState();
        if (data && data.token) {
            state.token = data.token;
            state.hasPredictions = Boolean(data.has_predictions);
            state.fileName = data.upload_filename || null;
        }
        updateActions();
        renderSummary(data);
    }

    document.addEventListener('DOMContentLoaded', () => {
        bind();
        init();
    });
})();
