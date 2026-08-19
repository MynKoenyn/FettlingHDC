document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.gantt-chart [data-x]').forEach(function (el) {
        el.style.left = el.dataset.x + '%';
        if (el.dataset.w !== undefined) {
            el.style.width = el.dataset.w + '%';
        }
    });
});
