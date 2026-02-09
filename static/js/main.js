/**
 * Global UI helpers for Telco Churn AI.
 */
(function () {
    const TOAST_DURATION = 4500;
    let toastContainer;

    function ensureToastContainer() {
        if (!toastContainer) {
            toastContainer = document.createElement('div');
            toastContainer.className = 'toast-container';
            document.body.appendChild(toastContainer);
        }
    }

    window.showToast = function ({ message, link, tone = 'info' } = {}) {
        if (!message) {
            return;
        }
        ensureToastContainer();
        const toast = document.createElement('div');
        toast.className = `toast toast-${tone}`;
        const textSpan = document.createElement('span');
        textSpan.textContent = message;
        toast.appendChild(textSpan);

        if (link && link.href && link.label) {
            const linkEl = document.createElement('a');
            linkEl.href = link.href;
            linkEl.textContent = link.label;
            linkEl.className = 'toast-link';
            linkEl.target = '_blank';
            linkEl.rel = 'noopener';
            toast.appendChild(linkEl);
        }

        toastContainer.appendChild(toast);
        requestAnimationFrame(() => {
            toast.classList.add('visible');
        });

        setTimeout(() => {
            toast.classList.remove('visible');
            setTimeout(() => toast.remove(), 220);
        }, TOAST_DURATION);
    };

    function autoDismissFlashes() {
        document.querySelectorAll('.flash-stack .flash').forEach((flash) => {
            setTimeout(() => {
                flash.style.transition = 'opacity 250ms ease, transform 250ms ease';
                flash.style.opacity = '0';
                flash.style.transform = 'translateY(-4px)';
                setTimeout(() => flash.remove(), 260);
            }, 4000);
        });
    }

    function handleNavToggle() {
        const toggle = document.querySelector('[data-nav-toggle]');
        const nav = document.querySelector('[data-nav-links]');
        if (!toggle || !nav) {
            return;
        }

        const closeNav = () => {
            nav.classList.remove('is-open');
            toggle.setAttribute('aria-expanded', 'false');
        };

        toggle.addEventListener('click', () => {
            const open = nav.classList.toggle('is-open');
            toggle.setAttribute('aria-expanded', String(open));
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth > 1024) {
                closeNav();
            }
        });

        document.addEventListener('click', (event) => {
            if (!nav.classList.contains('is-open')) {
                return;
            }
            if (event.target === toggle || toggle.contains(event.target)) {
                return;
            }
            if (!nav.contains(event.target)) {
                closeNav();
            }
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        ensureToastContainer();
        handleNavToggle();
        autoDismissFlashes();
    });
})();
