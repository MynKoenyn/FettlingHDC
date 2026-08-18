function hdcEnhanceSearchableSelects(root) {
    (root || document).querySelectorAll('select.searchable-select').forEach(function (sel) {
        if (sel.dataset.ssEnhanced) return;
        sel.dataset.ssEnhanced = '1';

        var wrap = document.createElement('div');
        wrap.className = 'ss-wrap';
        sel.parentNode.insertBefore(wrap, sel);
        wrap.appendChild(sel);
        sel.classList.add('ss-native');
        sel.tabIndex = -1;

        var input = document.createElement('input');
        input.type = 'text';
        input.className = sel.dataset.inputClass || 'app-select';
        input.autocomplete = 'off';
        input.placeholder = 'Type to search…';
        wrap.appendChild(input);
        sel._ssInput = input;

        var menu = document.createElement('div');
        menu.className = 'ss-menu';
        wrap.appendChild(menu);

        var activeIndex = -1;
        var visible = [];

        function options() {
            return Array.from(sel.options).map(function (o) { return { value: o.value, label: o.text }; });
        }
        function selectedLabel() {
            var o = sel.options[sel.selectedIndex];
            return o ? o.text : '';
        }
        function closeMenu() { menu.classList.remove('open'); }
        function setActive(i) {
            var opts = menu.querySelectorAll('.ss-option');
            opts.forEach(function (el) { el.classList.remove('active'); });
            if (opts[i]) { opts[i].classList.add('active'); opts[i].scrollIntoView({ block: 'nearest' }); }
            activeIndex = i;
        }
        function renderMenu(query) {
            var q = (query || '').toLowerCase().trim();
            visible = options().filter(function (o) { return o.label.toLowerCase().includes(q); });
            menu.innerHTML = '';
            if (!visible.length) {
                var empty = document.createElement('div');
                empty.className = 'ss-empty';
                empty.textContent = 'No matches';
                menu.appendChild(empty);
            } else {
                visible.forEach(function (o) {
                    var div = document.createElement('div');
                    div.className = 'ss-option' + (o.value === sel.value ? ' selected' : '');
                    div.textContent = o.label;
                    div.addEventListener('mousedown', function (e) {
                        e.preventDefault();
                        sel.value = o.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        input.value = o.label;
                        closeMenu();
                    });
                    menu.appendChild(div);
                });
            }
            activeIndex = -1;
            menu.classList.add('open');
        }

        input.value = selectedLabel();

        input.addEventListener('focus', function () {
            input.select();
            renderMenu('');
        });
        input.addEventListener('input', function () { renderMenu(input.value); });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (!menu.classList.contains('open')) { renderMenu(input.value); return; }
                setActive(Math.min(activeIndex + 1, menu.querySelectorAll('.ss-option').length - 1));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setActive(Math.max(activeIndex - 1, 0));
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeIndex >= 0 && visible[activeIndex]) {
                    sel.value = visible[activeIndex].value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    input.value = visible[activeIndex].label;
                }
                closeMenu();
            } else if (e.key === 'Escape') {
                input.value = selectedLabel();
                closeMenu();
            }
        });
        input.addEventListener('blur', function () {
            setTimeout(function () { input.value = selectedLabel(); closeMenu(); }, 120);
        });
    });
}
function hdcRefreshSearchableSelect(sel) {
    // Call after changing a searchable-select's options/value from
    // script — the visible search box otherwise keeps showing stale text.
    if (sel && sel._ssInput) {
        var o = sel.options[sel.selectedIndex];
        sel._ssInput.value = o ? o.text : '';
    }
}
// ── Admin nav dropdown ──────────────────────────────────────────
function hdcInitAdminMenu() {
    var trigger = document.getElementById('hdcAdminTrigger');
    var menu = document.getElementById('hdcAdminMenu');
    if (!trigger || !menu) return;

    function close() {
        menu.classList.remove('open');
        trigger.setAttribute('aria-expanded', 'false');
    }
    trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        var willOpen = !menu.classList.contains('open');
        menu.classList.toggle('open', willOpen);
        trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });
    document.addEventListener('click', function (e) {
        if (!e.target.closest('.hdc-admin')) close();
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') close();
    });
}

// ── Confirmation dialog — replaces native confirm() for destructive actions.
// Usage: add data-confirm-title / data-confirm-body / data-confirm-label to a
// <form> or a <button>/<a>. The dialog intercepts the first submit/click,
// then re-fires the original action once the user confirms.
function hdcConfirm(opts) {
    return new Promise(function (resolve) {
        var backdrop = document.createElement('div');
        backdrop.className = 'app-confirm-backdrop';
        backdrop.innerHTML =
            '<div class="app-confirm" role="alertdialog" aria-modal="true" aria-labelledby="hdcConfirmTitle">' +
            '<h3 id="hdcConfirmTitle"><i class="bi bi-exclamation-triangle-fill"></i>' + (opts.title || 'Are you sure?') + '</h3>' +
            '<p>' + (opts.body || 'This action cannot be undone.') + '</p>' +
            '<div class="row">' +
            '<button type="button" class="app-btn app-btn-outline" data-act="cancel">Cancel</button>' +
            '<button type="button" class="app-btn app-btn-danger" data-act="ok">' + (opts.confirmLabel || 'Confirm') + '</button>' +
            '</div></div>';
        document.body.appendChild(backdrop);
        requestAnimationFrame(function () { backdrop.classList.add('open'); });

        function done(result) {
            backdrop.classList.remove('open');
            setTimeout(function () { backdrop.remove(); }, 150);
            document.removeEventListener('keydown', onKey);
            resolve(result);
        }
        function onKey(e) { if (e.key === 'Escape') done(false); }
        document.addEventListener('keydown', onKey);
        backdrop.addEventListener('click', function (e) {
            if (e.target === backdrop) done(false);
        });
        backdrop.querySelector('[data-act="cancel"]').addEventListener('click', function () { done(false); });
        backdrop.querySelector('[data-act="ok"]').addEventListener('click', function () { done(true); });
        backdrop.querySelector('[data-act="ok"]').focus();
    });
}

function hdcInitConfirmGuards(root) {
    (root || document).querySelectorAll('form[data-confirm-body]').forEach(function (form) {
        if (form.dataset.confirmWired) return;
        form.dataset.confirmWired = '1';
        form.addEventListener('submit', function (e) {
            if (form.dataset.confirmed) return;
            e.preventDefault();
            hdcConfirm({
                title: form.dataset.confirmTitle,
                body: form.dataset.confirmBody,
                confirmLabel: form.dataset.confirmLabel,
            }).then(function (ok) {
                if (ok) { form.dataset.confirmed = '1'; form.requestSubmit ? form.requestSubmit() : form.submit(); }
            });
        });
    });
}

// ── Disable-on-submit guard — prevents double-submits on slow saves.
// Opt out per-form with data-no-guard.
function hdcInitSubmitGuards(root) {
    (root || document).querySelectorAll('form:not([data-no-guard])').forEach(function (form) {
        if (form.dataset.guardWired) return;
        form.dataset.guardWired = '1';
        form.addEventListener('submit', function () {
            if (form.dataset.confirmBody && !form.dataset.confirmed) return; // wait for confirm dialog first
            var btn = form.querySelector('button[type="submit"], input[type="submit"]');
            if (btn && !btn.classList.contains('is-loading')) {
                var textColor = getComputedStyle(btn).color;
                btn.style.setProperty('--spin-c-strong', textColor);
                btn.style.setProperty('--spin-c', 'color-mix(in srgb, ' + textColor + ' 35%, transparent)');
                btn.classList.add('is-loading');
                btn.disabled = true;
            }
        });
    });
}

document.addEventListener('DOMContentLoaded', function () {
    hdcEnhanceSearchableSelects();
    hdcInitAdminMenu();
    hdcInitConfirmGuards();
    hdcInitSubmitGuards();
});
