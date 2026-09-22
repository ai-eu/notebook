const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileinput');
const progressArea = document.getElementById('progress-area');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const recordingsContainer = document.getElementById('recordings');
const tagsFilterContainer = document.getElementById('tags-filter');
const voiceRow = document.getElementById('voice-row');
const voiceSelect = document.getElementById('voice-select');
const voiceHint = document.getElementById('voice-hint');

const ACTIVE_TAG_KEY = 'activeTag';
let activeTag = localStorage.getItem(ACTIVE_TAG_KEY) || '';
let currentRecordings = [];
// A text file waiting for the user to pick a voice before it is uploaded.
let pendingTtsFile = null;

function preventDefaults(e) {
    e.preventDefault();
    e.stopPropagation();
}

function highlight(zone, on) {
    zone.classList.toggle('dragover', on);
}

function setupDropzone(zone, fileInputEl, onFile) {
    if (!zone) return;
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        zone.addEventListener(eventName, preventDefaults, false);
    });
    ['dragenter', 'dragover'].forEach(eventName => {
        zone.addEventListener(eventName, () => highlight(zone, true), false);
    });
    ['dragleave', 'drop'].forEach(eventName => {
        zone.addEventListener(eventName, () => highlight(zone, false), false);
    });
    zone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length) onFile(files[0]);
    }, false);
    zone.addEventListener('click', () => fileInputEl && fileInputEl.click(), false);
    fileInputEl.addEventListener('change', (e) => {
        if (e.target.files.length) {
            onFile(e.target.files[0]);
            e.target.value = '';
        }
    });
}

setupDropzone(dropzone, fileInput, handleFile);

// --- TTS mode: detected from the uploaded file's format (.txt / .md) ---

let voicesLoaded = false;

function isTextFile(file) {
    const textTypes = ['text/plain', 'text/markdown'];
    if (textTypes.includes(file.type)) return true;
    return /\.(txt|md)$/i.test(file.name);
}

function handleFile(file) {
    if (!file) return;
    if (isTextFile(file)) {
        if (!voiceSelect) {
            // TTS is disabled server-side; no voice selector exists in the DOM.
            progressArea.classList.remove('hidden');
            progressFill.classList.add('error');
            progressFill.style.width = '100%';
            progressText.textContent = 'Text files require text-to-speech, which is disabled.';
            return;
        }
        pendingTtsFile = file;
        voiceRow.classList.remove('hidden');
        voiceHint.classList.remove('hidden');
        voiceSelect.classList.add('voice-required');
        loadVoices();
        return;
    }
    uploadFile(file);
}

voiceSelect?.addEventListener('change', () => {
    if (!pendingTtsFile) return;
    voiceHint.classList.add('hidden');
    voiceSelect.classList.remove('voice-required');
    const file = pendingTtsFile;
    pendingTtsFile = null;
    uploadTtsFile(file);
});

async function loadVoices() {
    try {
        const res = await fetch('/api/tts/voices');
        if (!res.ok) return;
        const data = await res.json();
        const groups = {};
        for (const voice of data.voices) {
            (groups[voice.lang] = groups[voice.lang] || []).push(voice);
        }
        const langNames = { en: 'English', 'pt-PT': 'Português', es: 'Español', uk: 'Українська' };
        // No voice is preselected: the upload starts only after the user picks one.
        voiceSelect.innerHTML =
            '<option value="" disabled selected>Select a voice…</option>' +
            Object.keys(groups).map(lang =>
                `<optgroup label="${escapeHtml(langNames[lang] || lang)}">` +
                groups[lang].map(v =>
                    `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`
                ).join('') +
                '</optgroup>'
            ).join('');
        voicesLoaded = true;
    } catch (e) { /* the selector just stays empty */ }
}

function formatDuration(seconds) {
    if (!seconds || isNaN(seconds)) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function statusLabel(status) {
    const labels = {
        pending: 'Queued',
        processing: 'Processing',
        done: 'Done',
        error: 'Error',
    };
    return labels[status] || status;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function formatDateTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const time = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
    return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()} ${time}`;
}

function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
}

function tagHue(tag) {
    let hue = 0;
    for (let i = 0; i < tag.length; i++) hue = (hue * 31 + tag.charCodeAt(i)) % 360;
    return hue;
}

function renderTags(tags) {
    if (!tags) return '';
    const chips = tags.split(/\s+/).filter(Boolean).map(tag =>
        `<span class="tag-chip" style="--tag-hue: ${tagHue(tag)}">${escapeHtml(tag)}</span>`
    ).join('');
    return `<div class="recording-tags">${chips}</div>`;
}

function uniqueTags(recordings) {
    const tags = new Set();
    for (const r of recordings) {
        for (const tag of (r.tags || '').split(/\s+/)) {
            if (tag) tags.add(tag);
        }
    }
    return [...tags].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
}

function setActiveTag(tag) {
    activeTag = tag;
    if (tag) localStorage.setItem(ACTIVE_TAG_KEY, tag);
    else localStorage.removeItem(ACTIVE_TAG_KEY);
}

function renderTagFilter(recordings) {
    if (!tagsFilterContainer) return;
    const tags = uniqueTags(recordings);
    // The saved filter is stale when its tag is gone from every recording
    if (activeTag && !tags.includes(activeTag)) setActiveTag('');
    if (!tags.length) {
        tagsFilterContainer.innerHTML = '';
        tagsFilterContainer.classList.add('hidden');
        return;
    }
    tagsFilterContainer.classList.remove('hidden');
    tagsFilterContainer.innerHTML = tags.map(tag =>
        `<button type="button" class="tag-chip${tag === activeTag ? ' active' : ''}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`
    ).join('');
}

function uploadFile(file) {
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    formData.append('last_modified', file.lastModified || Date.now());

    progressArea.classList.remove('hidden');
    progressFill.style.width = '0%';
    progressText.textContent = 'Uploading...';

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload', true);

    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = percent + '%';
            progressText.textContent = `Uploading: ${percent}%`;
        }
    });

    xhr.addEventListener('load', () => {
        if (xhr.status === 200) {
            const data = JSON.parse(xhr.responseText);
            progressText.textContent = 'Upload complete, processing...';
            pollStatus(data.recording_id);
            loadRecordings();
        } else {
            let detail = 'Upload error';
            try {
                const resp = JSON.parse(xhr.responseText);
                detail = resp.detail || detail;
            } catch (e) {}
            progressText.textContent = detail;
            progressFill.style.width = '100%';
            progressFill.classList.add('error');
        }
    });

    xhr.addEventListener('error', () => {
        progressText.textContent = 'Network error';
        progressFill.classList.add('error');
    });

    xhr.send(formData);
}

function uploadTtsFile(file) {
    if (!file) return;
    if (!voiceSelect || !voiceSelect.value) {
        // No voice chosen yet: prompt and wait instead of uploading.
        pendingTtsFile = file;
        voiceRow?.classList.remove('hidden');
        voiceHint?.classList.remove('hidden');
        voiceSelect?.classList.add('voice-required');
        if (!voicesLoaded) loadVoices();
        return;
    }
    const formData = new FormData();
    formData.append('file', file);
    if (voiceSelect && voiceSelect.value) formData.append('voice', voiceSelect.value);

    progressArea.classList.remove('hidden');
    progressFill.style.width = '0%';
    progressText.textContent = 'Uploading text...';

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/tts/upload', true);

    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = percent + '%';
            progressText.textContent = `Uploading: ${percent}%`;
        }
    });

    xhr.addEventListener('load', () => {
        if (xhr.status === 200) {
            const data = JSON.parse(xhr.responseText);
            progressText.textContent = 'Upload complete, synthesizing speech...';
            pollStatus(data.recording_id);
            loadRecordings();
        } else {
            let detail = 'Upload error';
            try {
                const resp = JSON.parse(xhr.responseText);
                detail = resp.detail || detail;
            } catch (e) {}
            progressText.textContent = detail;
            progressFill.style.width = '100%';
            progressFill.classList.add('error');
        }
    });

    xhr.addEventListener('error', () => {
        progressText.textContent = 'Network error';
        progressFill.classList.add('error');
    });

    xhr.send(formData);
}

const STAGE_LABELS = {
    denoising: 'Denoising',
    converting: 'Converting',
    transcribing: 'Transcribing',
    formatting: 'Formatting',
};

function formatElapsedTime(iso) {
    const started = new Date(iso).getTime();
    if (isNaN(started)) return '';
    const secs = Math.max(0, Math.floor((Date.now() - started) / 1000));
    return formatDuration(secs);
}

function pollStatus(recordingId) {
    const interval = setInterval(async () => {
        try {
            const res = await fetch(`/api/recordings/${recordingId}/status`);
            const data = await res.json();
            if (data.status === 'done' || data.status === 'error') {
                clearInterval(interval);
                progressArea.classList.add('hidden');
                progressFill.classList.remove('indeterminate');
                if (data.status === 'done') {
                    showToast('Transcription ready');
                    playChime();
                } else {
                    showToast(data.error_message || 'Processing failed', true);
                }
                loadRecordings();
                return;
            }
            let text = `Status: ${statusLabel(data.status)}`;
            let percent = null;
            const ttsProgress = data.tts_progress;
            if (ttsProgress && ttsProgress.chunks_total > 0) {
                percent = Math.round((ttsProgress.chunks_done / ttsProgress.chunks_total) * 100);
                text = `Synthesizing: ${ttsProgress.chunks_done}/${ttsProgress.chunks_total} chunks (${percent}%)`;
            } else if (data.progress && data.progress.stage) {
                const stage = data.progress.stage;
                text = STAGE_LABELS[stage] || 'Processing';
                if (stage === 'transcribing' && data.progress.chunks_total > 0) {
                    percent = Math.round((data.progress.chunks_done / data.progress.chunks_total) * 100);
                    text += `: ${data.progress.chunks_done}/${data.progress.chunks_total} chunks (${percent}%)`;
                } else {
                    const elapsed = formatElapsedTime(data.progress.started_at);
                    if (elapsed) text += `... ${elapsed}`;
                }
            }
            progressText.textContent = text;
            if (percent !== null) {
                progressFill.classList.remove('indeterminate');
                progressFill.style.width = percent + '%';
            } else {
                // No percent for this stage: keep the bar moving so the app looks alive.
                progressFill.classList.add('indeterminate');
            }
        } catch (e) {
            clearInterval(interval);
            progressFill.classList.remove('indeterminate');
        }
    }, 2000);
}

function showToast(message, isError = false) {
    if (!document.body) return;
    const toast = document.createElement('div');
    toast.className = 'toast' + (isError ? ' toast-error' : '');
    toast.setAttribute('role', 'status');
    toast.textContent = message;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 400);
    }, 6000);
}

function playChime() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        [660, 880].forEach((freq, i) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            const at = ctx.currentTime + i * 0.18;
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.06, at);
            gain.gain.exponentialRampToValueAtTime(0.001, at + 0.35);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(at);
            osc.stop(at + 0.4);
        });
    } catch (e) { /* sound is a nice-to-have, never a failure */ }
}

function renderCards(recordings) {
    recordingsContainer.innerHTML = recordings.map(r => {
        const duration = r.duration ? ` (${formatDuration(r.duration)})` : '';
        const title = r.original_filename || 'Untitled';
        const date = `${formatDateTime(r.created_at)}${duration}`;
        const tags = r.tags || '';
        const heading = r.status === 'done'
            ? `<a class="recording-link" href="/t/${r.recording_id}"><strong>${escapeHtml(title)}</strong><span class="recording-date">${date}</span></a>`
            : `<strong>${escapeHtml(title)}</strong><span class="recording-date">${date}</span>`;
        const status = r.status === 'done' ? '' : `<span class="recording-status status-${r.status}">${statusLabel(r.status)}</span>`;
        const error = r.status === 'error' && r.error_message ? `<span class="error">${escapeHtml(r.error_message)}</span>` : '';
        return `
            <div class="recording-row" data-id="${r.recording_id}">
                <div class="recording-info">${heading}${status}${error}${renderTags(tags)}</div>
                <button class="delete-recording" data-id="${r.recording_id}" title="Delete">&times;</button>
                <button class="tags-btn" data-id="${r.recording_id}" title="Tags" aria-label="Edit tags">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.83z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
                </button>
                <div class="tags-popup hidden" data-id="${r.recording_id}">
                    <input type="text" class="tags-input" placeholder="tag1 tag2" value="${escapeHtml(tags)}" aria-label="Tags">
                    <textarea class="comment-input" rows="3" placeholder="Comment">${escapeHtml(r.comment || '')}</textarea>
                    <div class="tags-popup-actions">
                        <button type="button" class="tags-cancel">Cancel</button>
                        <button type="button" class="tags-save" data-id="${r.recording_id}">Save</button>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function renderRecordings(recordings) {
    if (!recordingsContainer) return;
    currentRecordings = recordings;
    renderTagFilter(recordings);
    const visible = activeTag
        ? recordings.filter(r => (r.tags || '').split(/\s+/).includes(activeTag))
        : recordings;
    if (!visible.length) {
        recordingsContainer.innerHTML = activeTag
            ? '<p>No recordings with this tag.</p>'
            : '<p>No recordings yet.</p>';
        return;
    }
    renderCards(visible);
}

async function loadRecordings() {
    try {
        const res = await fetch('/api/recordings');
        const data = await res.json();
        renderRecordings(data);
    } catch (e) {
        if (recordingsContainer) recordingsContainer.innerHTML = '<p class="error">Could not load the list.</p>';
    }
}

function closeTagsPopups() {
    recordingsContainer?.querySelectorAll('.tags-popup').forEach(p => p.classList.add('hidden'));
}

async function saveTags(recordingId, popup) {
    const tags = popup.querySelector('.tags-input').value;
    const comment = popup.querySelector('.comment-input').value;
    try {
        const res = await fetch(`/api/recordings/${recordingId}/tags`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags, comment }),
        });
        if (!res.ok) throw new Error('Save error');
        closeTagsPopups();
        loadRecordings();
    } catch (e) {
        alert('Could not save the tags');
    }
}

if (recordingsContainer) {
    recordingsContainer.addEventListener('click', async (e) => {
        const saveBtn = e.target.closest('.tags-save');
        if (saveBtn) {
            await saveTags(saveBtn.dataset.id, saveBtn.closest('.tags-popup'));
            return;
        }
        if (e.target.closest('.tags-cancel')) {
            closeTagsPopups();
            return;
        }
        const tagsBtn = e.target.closest('.tags-btn');
        if (tagsBtn) {
            const popup = tagsBtn.closest('.recording-row').querySelector('.tags-popup');
            const wasOpen = !popup.classList.contains('hidden');
            closeTagsPopups();
            if (!wasOpen) {
                popup.classList.remove('hidden');
                popup.querySelector('.tags-input').focus();
            }
            return;
        }
        const btn = e.target.closest('.delete-recording');
        if (!btn) return;
        e.preventDefault();
        const recordingId = btn.dataset.id;
        if (!confirm('Delete this recording and transcript permanently?')) return;
        try {
            const res = await fetch(`/api/recordings/${recordingId}`, { method: 'DELETE' });
            if (!res.ok) throw new Error('Delete error');
            loadRecordings();
        } catch (e) {
            alert('Could not delete the recording');
        }
    });
}

if (tagsFilterContainer) {
    tagsFilterContainer.addEventListener('click', (e) => {
        const chip = e.target.closest('.tag-chip');
        if (!chip) return;
        setActiveTag(chip.dataset.tag === activeTag ? '' : chip.dataset.tag);
        renderRecordings(currentRecordings);
    });
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.tags-popup') && !e.target.closest('.tags-btn')) closeTagsPopups();
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTagsPopups();
});

loadRecordings();
