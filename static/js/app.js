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
        const error = r.status === 'error' ? `<span class="error"> — ${r.error_message || 'error'}</span>` : '';
        return `
            <div class="recording-row" data-id="${r.recording_id}">
                <div class="recording-info">
                    <strong>${r.original_filename || 'Untitled'}</strong>
                    <span class="recording-date">${new Date(r.created_at).toLocaleString()}${duration}</span>
                    <span class="recording-status status-${r.status}">${statusLabel(r.status)}</span>
                    ${error}
                </div>
                <div class="recording-actions">
                    ${r.status === 'done' ? `<a href="/t/${r.recording_id}">Transcript</a>` : ''}
                    <button class="delete-recording" data-id="${r.recording_id}" title="Delete">&times;</button>
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

if (recordingsContainer) {
    recordingsContainer.addEventListener('click', async (e) => {
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

loadRecordings();
