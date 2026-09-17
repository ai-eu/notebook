const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileinput');
const progressArea = document.getElementById('progress-area');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const recordingsContainer = document.getElementById('recordings');

if (dropzone) {
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropzone.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    ['dragenter', 'dragover'].forEach(eventName => {
        dropzone.addEventListener(eventName, () => dropzone.classList.add('dragover'), false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropzone.addEventListener(eventName, () => dropzone.classList.remove('dragover'), false);
    });

    dropzone.addEventListener('drop', handleDrop, false);
    dropzone.addEventListener('click', () => fileInput && fileInput.click(), false);
}

if (fileInput) {
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length) {
            uploadFile(e.target.files[0]);
        }
    });
}

function handleDrop(e) {
    const files = e.dataTransfer.files;
    if (files.length) {
        uploadFile(files[0]);
    }
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

function pollStatus(recordingId) {
    const interval = setInterval(async () => {
        try {
            const res = await fetch(`/api/recordings/${recordingId}/status`);
            const data = await res.json();
            if (data.status === 'done' || data.status === 'error') {
                clearInterval(interval);
                progressArea.classList.add('hidden');
                loadRecordings();
            } else {
                progressText.textContent = `Status: ${statusLabel(data.status)}`;
            }
        } catch (e) {
            clearInterval(interval);
        }
    }, 2000);
}

function renderRecordings(recordings) {
    if (!recordingsContainer) return;
    if (!recordings.length) {
        recordingsContainer.innerHTML = '<p>No recordings yet.</p>';
        return;
    }
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
            btn.closest('.recording-row')?.remove();
            if (!recordingsContainer.querySelectorAll('.recording-row').length) {
                recordingsContainer.innerHTML = '<p>No recordings yet.</p>';
            }
        } catch (e) {
            alert('Could not delete the recording');
        }
    });
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.tags-popup') && !e.target.closest('.tags-btn')) closeTagsPopups();
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTagsPopups();
});

loadRecordings();
