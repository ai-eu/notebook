const player = document.getElementById('player');
const transcriptContainer = document.getElementById('transcript');
const statusEl = document.getElementById('status');
const prevPhraseBtn = document.getElementById('prev-phrase');
const nextPhraseBtn = document.getElementById('next-phrase');
const downloadLink = document.getElementById('download-link');
const downloadLoader = document.getElementById('download-loader');
const audioHint = document.getElementById('audio-hint');

let sentences = [];
let activeSentenceIndex = -1;

// Audio that lives in the archive is downloaded on the first listen, which takes a
// moment: say so instead of leaving a silent player on screen.
const ARCHIVE_HINT_DELAY_MS = 800;
let archiveHintTimer = null;

function showAudioHint(text) {
    if (!audioHint) return;
    audioHint.textContent = text;
    audioHint.classList.remove('hidden');
}

function hideAudioHint() {
    if (archiveHintTimer) {
        clearTimeout(archiveHintTimer);
        archiveHintTimer = null;
    }
    if (audioHint) audioHint.classList.add('hidden');
}

player.addEventListener('waiting', () => {
    if (archiveHintTimer || !player.paused) return;
    archiveHintTimer = setTimeout(() => {
        archiveHintTimer = null;
        showAudioHint('Loading the audio from the archive…');
    }, ARCHIVE_HINT_DELAY_MS);
});

player.addEventListener('canplay', hideAudioHint);
player.addEventListener('playing', hideAudioHint);

player.addEventListener('error', () => {
    hideAudioHint();
    showAudioHint('The audio is not available right now. Reload the page to try again.');
});

function formatDuration(seconds) {
    if (!seconds || isNaN(seconds)) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function splitIntoSentences(text) {
    const matches = text.match(/[^.!?]+(?:[.!?]+|$)/g);
    return matches ? matches.map(s => s.trim()).filter(Boolean) : [text.trim()];
}

const PARAGRAPH_GAP_SECONDS = 1.5;

function renderTranscript(transcript) {
    const target = document.getElementById('original-sentences') || transcriptContainer;
    activeSentenceIndex = -1;

    // The server renders the column from formatted.txt and ships the very same
    // sentence list here, so the player and the translation share the indexes.
    const units = Array.isArray(window.SENTENCE_UNITS) ? window.SENTENCE_UNITS : [];
    if (units.length) {
        sentences = units.map((unit) => ({
            text: unit.text,
            start: Number(unit.start) || 0,
            end: Number(unit.end) || 0,
        }));
        return;
    }

    if (!transcript) {
        if (target === transcriptContainer) {
            target.innerHTML = '<p class="status-text">Transcript is still being processed...</p>';
        }
        return;
    }

    const segments = transcript.segments || [];
    sentences = [];
    target.innerHTML = '';

    let currentParagraph = null;
    let prevEnd = null;

    for (const seg of segments) {
        const text = (seg.text || '').trim();
        if (!text) continue;

        // New paragraph if there is a long pause between segments
        if (prevEnd !== null && seg.start - prevEnd > PARAGRAPH_GAP_SECONDS) {
            currentParagraph = null;
        }
        if (!currentParagraph) {
            currentParagraph = document.createElement('div');
            currentParagraph.className = 'transcript-paragraph';
            target.appendChild(currentParagraph);
        }

        const parts = splitIntoSentences(text);
        const totalLen = text.length;
        let cumLen = 0;

        for (let i = 0; i < parts.length; i++) {
            const sent = parts[i];
            const len = sent.length;
            const start = seg.start + (cumLen / (totalLen || 1)) * (seg.end - seg.start);
            const end = seg.start + ((cumLen + len) / (totalLen || 1)) * (seg.end - seg.start);
            const idx = sentences.length;
            sentences.push({ text: sent, start, end });

            const p = document.createElement('p');
            p.className = 'sentence sentence-line';
            p.dataset.index = idx;
            p.dataset.idx = idx;
            p.dataset.start = start.toFixed(3);
            p.dataset.end = end.toFixed(3);
            p.textContent = sent;
            p.addEventListener('click', () => playPhrase(idx));
            currentParagraph.appendChild(p);

            cumLen += len;
        }

        prevEnd = seg.end;
    }
}

function findActiveSentenceIndex(time) {
    if (!sentences.length) return -1;

    // Binary search: sentences are ordered by start.
    let low = 0;
    let high = sentences.length - 1;
    let candidate = -1;
    while (low <= high) {
        const mid = (low + high) >> 1;
        if (sentences[mid].start <= time) {
            candidate = mid;
            low = mid + 1;
        } else {
            high = mid - 1;
        }
    }

    if (candidate >= 0 && time < sentences[candidate].end) {
        return candidate;
    }
    const last = sentences[sentences.length - 1];
    if (last && last.end > last.start && time >= last.end) {
        return sentences.length - 1;
    }
    return -1;
}

function scrollToSentence(idx) {
    const el = document.querySelector(`.sentence-line[data-idx="${idx}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function playPhrase(idx) {
    if (idx < 0 || idx >= sentences.length) return;
    player.currentTime = sentences[idx].start;
    if (player.paused) {
        player.play().catch(() => {});
    }
}

function getPrevPhraseIdx() {
    const active = findActiveSentenceIndex(player.currentTime);
    if (active > 0) return active - 1;
    for (let i = sentences.length - 1; i >= 0; i--) {
        if (player.currentTime > sentences[i].end) return i;
    }
    return -1;
}

function getNextPhraseIdx() {
    const active = findActiveSentenceIndex(player.currentTime);
    if (active >= 0 && active < sentences.length - 1) return active + 1;
    for (let i = 0; i < sentences.length; i++) {
        if (player.currentTime < sentences[i].start) return i;
    }
    return -1;
}

function sentenceElement(idx) {
    return document.querySelector(`.sentence-line[data-idx="${idx}"]`);
}

function highlightSentence(idx) {
    if (idx === activeSentenceIndex) return;

    if (activeSentenceIndex >= 0) {
        sentenceElement(activeSentenceIndex)?.classList.remove('active');
    }
    activeSentenceIndex = idx;

    if (idx >= 0) {
        sentenceElement(idx)?.classList.add('active');
    }
}

player.addEventListener('timeupdate', () => {
    highlightSentence(findActiveSentenceIndex(player.currentTime));
});

player.addEventListener('seeked', () => {
    const idx = findActiveSentenceIndex(player.currentTime);
    highlightSentence(idx);
    if (idx >= 0) {
        scrollToSentence(idx);
    }
});

function statusLabel(status) {
    const labels = { pending: 'Queued', processing: 'Processing', done: 'Done', error: 'Error' };
    return labels[status] || status;
}

function pollStatus() {
    if (window.TRANSCRIPT && window.TXT_READY) return;
    const interval = setInterval(async () => {
        try {
            const res = await fetch(`/api/recordings/${window.RECORDING_ID}/status`);
            const data = await res.json();
            if (statusEl) statusEl.textContent = `${window.ORIGINAL_FILENAME} — ${statusLabel(data.status)}`;
            if (data.txt_ready && downloadLink) {
                window.TXT_READY = true;
                downloadLink.classList.remove('hidden');
            }
            if (data.status === 'done' || data.status === 'error') {
                clearInterval(interval);
                if (!window.TRANSCRIPT) window.location.reload();
            }
        } catch (e) {}
    }, 2000);
}

if (prevPhraseBtn) {
    prevPhraseBtn.addEventListener('click', () => {
        const idx = getPrevPhraseIdx();
        if (idx >= 0) playPhrase(idx);
    });
}

if (nextPhraseBtn) {
    nextPhraseBtn.addEventListener('click', () => {
        const idx = getNextPhraseIdx();
        if (idx >= 0) playPhrase(idx);
    });
}

function getFilenameFromHeaders(response) {
    const disposition = response.headers.get('Content-Disposition');
    if (!disposition) return 'transcript.txt';
    const match = disposition.match(/filename="([^"]+)"/);
    return match ? match[1] : 'transcript.txt';
}

async function handleDownload(e) {
    if (!downloadLink) return;
    e.preventDefault();
    if (downloadLink.classList.contains('loading')) return;

    downloadLink.classList.add('loading');
    if (downloadLoader) downloadLoader.classList.remove('hidden');

    try {
        const res = await fetch(downloadLink.href);
        if (!res.ok) throw new Error('Download failed');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = getFilenameFromHeaders(res);
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (err) {
        alert('Could not download the file. Please try again.');
    } finally {
        downloadLink.classList.remove('loading');
        if (downloadLoader) downloadLoader.classList.add('hidden');
    }
}

if (downloadLink) {
    downloadLink.addEventListener('click', handleDownload);
}

if (statusEl) {
    statusEl.textContent = window.ORIGINAL_FILENAME;
}
renderTranscript(window.TRANSCRIPT);
pollStatus();
