const themeToggle = document.getElementById('theme-toggle');
let transitionTimer;

if (themeToggle && window.applyTheme) {
    const isDark = () => document.documentElement.dataset.theme === 'dark';

    function syncThemeToggle() {
        const next = isDark() ? 'light' : 'dark';
        const label = `Switch to ${next} theme`;
        themeToggle.setAttribute('aria-label', label);
        themeToggle.title = label;
    }

    themeToggle.addEventListener('click', () => {
        const root = document.documentElement;
        root.classList.add('theme-transition');
        clearTimeout(transitionTimer);
        transitionTimer = setTimeout(() => root.classList.remove('theme-transition'), 250);

        window.applyTheme(isDark() ? 'light' : 'dark', true);
        syncThemeToggle();
    });

    syncThemeToggle();
}
