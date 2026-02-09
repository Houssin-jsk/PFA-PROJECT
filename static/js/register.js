(function () {
    const card = document.querySelector('[data-signup-card]');
    if (!card) {
        return;
    }

    const alertBox = card.querySelector('[data-alert]');
    const step1Form = card.querySelector('form[data-step="1"]');
    const step2Form = card.querySelector('form[data-step="2"]');
    const indicators = Array.from(card.querySelectorAll('[data-step-indicator]'));

    const state = {
        step: 1,
        orgId: null,
    };

    function clearInvalid(form) {
        if (!form) return;
        form.querySelectorAll('.is-invalid').forEach((el) => {
            el.classList.remove('is-invalid');
            el.removeAttribute('aria-invalid');
        });
    }

    function applyInvalid(form, fields) {
        clearInvalid(form);
        if (!form || !fields) return;
        Object.keys(fields).forEach((name) => {
            const input = form.querySelector(`[name="${CSS.escape(name)}"]`);
            if (!input) return;
            input.classList.add('is-invalid');
            input.setAttribute('aria-invalid', 'true');
        });
    }

    function setAlert(message, tone = 'info') {
        if (!alertBox) {
            return;
        }
        if (!message) {
            alertBox.classList.add('hidden');
            alertBox.textContent = '';
            alertBox.dataset.tone = '';
            return;
        }
        alertBox.textContent = message;
        alertBox.dataset.tone = tone;
        alertBox.classList.remove('hidden');
    }

    function setStep(step) {
        state.step = step;
        setAlert('');
        if (step1Form) step1Form.classList.toggle('hidden', step !== 1);
        if (step2Form) step2Form.classList.toggle('hidden', step !== 2);
        indicators.forEach((indicator) => {
            const indicatorStep = Number(indicator.getAttribute('data-step-indicator'));
            indicator.classList.toggle('is-active', indicatorStep === step);
            indicator.classList.toggle('is-done', indicatorStep < step);
        });
    }

    function setLoading(form, loading, label) {
        if (!form) return;
        const btn = form.querySelector('[data-primary-btn]');
        if (!btn) return;
        if (loading) {
            btn.disabled = true;
            btn.dataset.originalLabel = btn.textContent.trim();
            btn.textContent = label || 'Veuillez patienter…';
            btn.classList.add('is-loading');
        } else {
            btn.disabled = false;
            btn.classList.remove('is-loading');
            if (btn.dataset.originalLabel) {
                btn.textContent = btn.dataset.originalLabel;
            }
        }
    }

    async function postJson(url, payload) {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {}),
        });
        const data = await response.json().catch(() => ({}));
        return { ok: response.ok, status: response.status, data };
    }

    function collectForm(form) {
        const fd = new FormData(form);
        const payload = {};
        fd.forEach((value, key) => {
            payload[key] = typeof value === 'string' ? value.trim() : value;
        });
        return payload;
    }

    function passwordMeetsRules(password) {
        const value = String(password || '');
        const rules = [
            value.length >= 8,
            /[A-Z]/.test(value),
            /[a-z]/.test(value),
            /\d/.test(value),
        ];
        return rules.every(Boolean);
    }

    async function handleStep1(event) {
        event.preventDefault();
        setAlert('');
        clearInvalid(step1Form);
        if (!step1Form.reportValidity()) {
            return;
        }
        const payload = collectForm(step1Form);
        setLoading(step1Form, true, "Création…");
        try {
            const result = await postJson('/api/orgs', payload);
            if (!result.ok) {
                const message = result.data && result.data.message ? result.data.message : "Impossible de créer l'organisation.";
                if (result.data && result.data.fields) {
                    applyInvalid(step1Form, result.data.fields);
                }
                setAlert(message, 'error');
                return;
            }
            state.orgId = result.data.org && result.data.org.id ? result.data.org.id : null;
            if (window.showToast) {
                window.showToast({ message: "Organisation créée. Passez à l'utilisateur principal.", tone: 'success' });
            }
            setStep(2);
        } finally {
            setLoading(step1Form, false);
        }
    }

    async function handleStep2(event) {
        event.preventDefault();
        setAlert('');
        clearInvalid(step2Form);
        if (!step2Form.reportValidity()) {
            return;
        }

        const payload = collectForm(step2Form);
        payload.org_id = state.orgId;

        if (!passwordMeetsRules(payload.password)) {
            setAlert("Mot de passe trop faible. Min. 8 caractères avec majuscule, minuscule et chiffre.", 'error');
            return;
        }
        if (payload.password !== payload.confirm_password) {
            setAlert("Les mots de passe ne correspondent pas.", 'error');
            return;
        }

        setLoading(step2Form, true, 'Création…');
        try {
            const result = await postJson('/api/users', payload);
            if (!result.ok) {
                const message = result.data && result.data.message ? result.data.message : "Impossible de créer l'utilisateur.";
                if (result.data && result.data.fields) {
                    applyInvalid(step2Form, result.data.fields);
                }
                setAlert(message, 'error');
                return;
            }
            if (window.showToast) {
                window.showToast({ message: "Compte créé. Vous pouvez vous connecter.", tone: 'success' });
            }
            const redirectUrl = result.data.redirect_url || '/login';
            window.location.assign(redirectUrl);
        } finally {
            setLoading(step2Form, false);
        }
    }

    function bind() {
        if (step1Form) step1Form.addEventListener('submit', handleStep1);
        if (step2Form) step2Form.addEventListener('submit', handleStep2);
        const backBtn = card.querySelector('[data-back-btn]');
        if (backBtn) {
            backBtn.addEventListener('click', () => setStep(1));
        }
    }

    setStep(1);
    bind();
})();
