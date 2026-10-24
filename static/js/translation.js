// Translation UI: toggle column, language selector, sentence highlighting.

(function () {
    const toggleBtn = document.getElementById("toggle-translation");
    const langSelect = document.getElementById("translation-lang");
    const hint = document.getElementById("translation-hint");
    const translationColumn = document.getElementById("translation-column");
    const translatedSentences = document.getElementById("translated-sentences");
    const originalSentences = document.getElementById("original-sentences");
    const translationLabel = document.getElementById("translation-label");
    const columnHeader = document.getElementById("translation-column-header");

    if (!toggleBtn || !window.TRANSLATION_ENABLED) return;

    const defaultLang = window.TRANSLATION_DEFAULT_LANGUAGE || "uk";
    const languages = window.TRANSLATION_LANGUAGES || [];
    const languageNames = window.TRANSLATION_LANGUAGE_NAMES || {};
    const recordingId = window.RECORDING_ID;

    // Persisted state: which language is shown and whether translation is visible.
    const VISIBLE_KEY = `translation_visible_${recordingId}`;
    const LANG_KEY = `translation_lang_${recordingId}`;

    let visible = localStorage.getItem(VISIBLE_KEY) === "true";
    let currentLang = localStorage.getItem(LANG_KEY) || defaultLang;

    // Highlight pairing: index of the currently highlighted sentence.
    let highlightedIndex = -1;

    // --- Initialization ---

    function init() {
        // Populate language selector (may already be rendered by Jinja2).
        if (langSelect && langSelect.children.length === 0) {
            for (const code of languages) {
                const opt = document.createElement("option");
                opt.value = code;
                opt.textContent = languageNames[code] || code;
                if (code === currentLang) opt.selected = true;
                langSelect.appendChild(opt);
            }
        }

        if (langSelect) {
            langSelect.value = currentLang;
            langSelect.addEventListener("change", () => {
                currentLang = langSelect.value;
                localStorage.setItem(LANG_KEY, currentLang);
                loadTranslation(currentLang);
            });
        }

        updateToggleLabel();
        toggleBtn.addEventListener("click", toggleTranslation);

        if (visible) {
            showTranslation();
        } else {
            hideTranslation();
        }

        // Highlight pairing on hover over original sentences.
        originalSentences?.addEventListener("mouseover", (e) => {
            const sentence = e.target.closest(".sentence");
            if (!sentence) return;
            const idx = Number(sentence.dataset.index);
            highlightPair(idx);
        });

        originalSentences?.addEventListener("mouseout", () => {
            highlightPair(-1);
        });

        translatedSentences?.addEventListener("mouseover", (e) => {
            const sentence = e.target.closest(".sentence");
            if (!sentence) return;
            const idx = Number(sentence.dataset.index);
            highlightPair(idx);
        });

        translatedSentences?.addEventListener("mouseout", () => {
            highlightPair(-1);
        });
    }

    function updateToggleLabel() {
        if (translationLabel) {
            translationLabel.textContent = languageNames[currentLang] || currentLang;
        }
        if (columnHeader) {
            columnHeader.textContent = languageNames[currentLang] || currentLang;
        }
        toggleBtn.setAttribute("aria-pressed", visible ? "true" : "false");
        toggleBtn.classList.toggle("active", visible);
    }

    function toggleTranslation() {
        visible = !visible;
        localStorage.setItem(VISIBLE_KEY, String(visible));
        if (visible) {
            showTranslation();
        } else {
            hideTranslation();
        }
        updateToggleLabel();
    }

    function showTranslation() {
        if (!translationColumn) return;
        translationColumn.classList.remove("hidden");
        langSelect?.classList.remove("hidden");
        visible = true;
        localStorage.setItem(VISIBLE_KEY, "true");
        updateToggleLabel();
        if (translatedSentences.children.length === 0) {
            loadTranslation(currentLang);
        }
    }

    function hideTranslation() {
        if (!translationColumn) return;
        translationColumn.classList.add("hidden");
        langSelect?.classList.add("hidden");
        visible = false;
        localStorage.setItem(VISIBLE_KEY, "false");
        updateToggleLabel();
    }

    function sentenceAt(container, index) {
        return container?.querySelector(`.sentence[data-index="${index}"]`) || null;
    }

    function highlightPair(index) {
        highlightedIndex = index;
        for (const el of document.querySelectorAll(".sentence")) {
            el.classList.remove("highlighted");
        }
        if (index < 0) return;
        sentenceAt(originalSentences, index)?.classList.add("highlighted");
        sentenceAt(translatedSentences, index)?.classList.add("highlighted");
    }

    // --- Loading translation ---

    async function loadTranslation(lang) {
        if (!translatedSentences) return;
        translatedSentences.textContent = "";
        hint.classList.add("hidden");

        try {
            const res = await fetch(
                `/api/translate/${encodeURIComponent(recordingId)}?lang=${encodeURIComponent(lang)}`
            );
            if (!res.ok) {
                if (res.status === 404) {
                    hint.textContent = "Перевод недоступен для этой записи.";
                    hint.classList.remove("hidden");
                    return;
                }
                throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            const sentences = data.sentences || [];

            translatedSentences.innerHTML = sentences
                .map(
                    (text, idx) =>
                        `<div class="sentence" data-index="${idx}">${escapeHtml(text)}</div>`
                )
                .join("");

            highlightPair(highlightedIndex);
        } catch (exc) {
            hint.textContent = "Не удалось загрузить перевод: " + String(exc);
            hint.classList.remove("hidden");
        }
    }

    function escapeHtml(text) {
        return String(text ?? "").replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }[ch]));
    }

    init();
})();
