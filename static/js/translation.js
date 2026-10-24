// Translation UI: language dropdown and sentence highlighting.

(function () {
    const langSelect = document.getElementById("translation-lang");
    const hint = document.getElementById("translation-hint");
    const translationColumn = document.getElementById("translation-column");
    const translatedSentences = document.getElementById("translated-sentences");
    const originalSentences = document.getElementById("original-sentences");

    if (!langSelect || !window.TRANSLATION_ENABLED) return;

    const recordingId = window.RECORDING_ID;

    // Highlight pairing: index of the currently highlighted sentence.
    let highlightedIndex = -1;

    // Language of the sentences currently rendered in the translation column.
    let loadedLang = null;

    // --- Initialization ---

    function init() {
        // No language is selected by default: the translation stays hidden until
        // the reader picks one, even when a cached translation exists.
        langSelect.value = "";
        hideTranslation();

        langSelect.addEventListener("change", () => {
            const lang = langSelect.value;
            if (!lang) {
                hideTranslation();
                return;
            }
            showTranslation();
            if (lang !== loadedLang) {
                loadTranslation(lang);
            }
        });

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

    function showTranslation() {
        translationColumn?.classList.remove("hidden");
        hint?.classList.add("hidden");
    }

    function hideTranslation() {
        translationColumn?.classList.add("hidden");
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

        try {
            const res = await fetch(
                `/api/translate/${encodeURIComponent(recordingId)}?lang=${encodeURIComponent(lang)}`
            );
            if (!res.ok) {
                fail(
                    res.status === 404
                        ? "Перевод недоступен для этой записи."
                        : `Не удалось загрузить перевод: HTTP ${res.status}`
                );
                return;
            }
            const data = await res.json();
            const sentences = data.sentences || [];

            translatedSentences.innerHTML = renderSentences(sentences, data.paragraphs);

            loadedLang = lang;
            highlightPair(highlightedIndex);
        } catch (exc) {
            fail("Не удалось загрузить перевод: " + String(exc));
        }
    }

    // Nothing to show: hide the column and return the dropdown to its empty state.
    function fail(message) {
        loadedLang = null;
        langSelect.value = "";
        hideTranslation();
        hint.textContent = message;
        hint.classList.remove("hidden");
    }

    // Translated sentences keep the paragraph structure of the original column,
    // so the two columns stay visually aligned line by line.
    function renderSentences(sentences, paragraphs) {
        const paragraphOf = (idx) =>
            Array.isArray(paragraphs) && Number.isInteger(paragraphs[idx]) ? paragraphs[idx] : 0;

        let html = "";
        let currentParagraph = -1;
        sentences.forEach((text, idx) => {
            const paragraph = paragraphOf(idx);
            if (paragraph !== currentParagraph) {
                if (currentParagraph !== -1) html += "</div>";
                html += '<div class="transcript-paragraph">';
                currentParagraph = paragraph;
            }
            html += `<div class="sentence" data-index="${idx}">${escapeHtml(text)}</div>`;
        });
        if (currentParagraph !== -1) html += "</div>";
        return html;
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
