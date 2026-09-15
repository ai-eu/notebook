# Dictaphone / iPhone Audio Transcription Service

FastAPI web app for drag-and-drop audio/video upload, automatic MP3 conversion, Groq Whisper transcription, and a transcript player with sentence highlighting.

## Features

- Drag-and-drop upload of audio/video files.
- Conversion to MP3 mono 48 kbps (~0.36 MB/min).
- MP3 files with a bitrate up to 196 kbps are copied as-is (`-c:a copy`) to avoid re-encoding already-compressed audio.
- Automatic chunking of long recordings: chunk duration is chosen based on the output audio bitrate so each chunk fits `TARGET_CHUNK_MB` and Groq's 25 MB limit.
- Supports audio/video formats including Apple files: `.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.aac`, `.caf`, `.aif`, `.aiff`, `.wma`, `.amr`, `.3gp`, `.webm`, `.mp4`, `.mov`, `.mkv`.
- SQLite database with relative recording folder paths (`<user_id>/<recording_id>`) so the `data/` directory and database can be moved between servers.
- Invitation-key authentication; admin keys can view all users' recordings.
- Optional Groq-powered TXT formatting (`SMART_FORMAT`) for splitting long lines.

## Requirements

- Python 3.11+
- `ffmpeg` + `ffprobe`
- Debian/Ubuntu VPS (for deployment)
- Groq API key: https://console.groq.com/keys

## Local run (Windows / Linux)

```bash
# Clone/copy the project
cd dictaphone

# Create .env from the example and fill in GROQ_API_KEY
cp .env.example .env
# edit .env

python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

pip install -r requirements.txt

# Create the first admin/user key
python -m app.cli create-key "Admin" --admin

# Run (no reload, so background tasks are not killed)
venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
# Linux: venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and enter the key.

## Test mode without Groq

For local UI testing without a real key:

```bash
MOCK_TRANSCRIPTION=true venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Key management (CLI)

```bash
python -m app.cli create-key "User name"
python -m app.cli create-key "Admin" --admin
python -m app.cli list-keys
python -m app.cli revoke-key <user_id>
```

## Administration

Keys created with `--admin` can view all users' recordings through the API:

```bash
# List all recordings (GET)
curl -b cookies.txt "http://localhost:8000/api/admin/recordings"

# View any recording transcript (GET)
curl -b cookies.txt "http://localhost:8000/api/admin/recordings/{recording_id}"

# Download TXT for any recording (GET)
curl -b cookies.txt "http://localhost:8000/api/admin/recordings/{recording_id}/download"
```

Users without the `--admin` flag see only their own recordings.

## Data structure

```
data/
  └── <user_id>/
        └── <recording_id>/
              ├── audio.mp3
              └── transcript.json
```

Each upload gets its own folder with the recording timestamp in the name.

## Deployment on Debian VPS

1. Prepare the server:

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv ffmpeg nginx certbot python3-certbot-nginx
```

2. Copy the project:

```bash
sudo mkdir -p /opt/dictaphone
sudo cp -r . /opt/dictaphone
sudo chown -R www-data:www-data /opt/dictaphone
```

3. Configure the environment:

```bash
cd /opt/dictaphone
sudo -u www-data cp .env.example .env
# edit .env: GROQ_API_KEY, SESSION_COOKIE_SECURE=true

sudo -u www-data python3.11 -m venv venv
sudo -u www-data venv/bin/pip install -r requirements.txt
```

4. Create the first key:

```bash
sudo -u www-data venv/bin/python -m app.cli create-key "Admin" --admin
```

5. Systemd:

```bash
sudo cp deploy/dictaphone.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dictaphone
```

6. Nginx + HTTPS:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/dictaphone
sudo ln -s /etc/nginx/sites-available/dictaphone /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# Replace example.com with your domain
sudo certbot --nginx -d example.com
```
