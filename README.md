# Dictaphone / iPhone Audio Transcription Service

FastAPI web app for drag-and-drop audio/video upload, automatic MP3 conversion, Groq Whisper transcription, and a transcript player with sentence highlighting.

## Features

- Drag-and-drop upload of audio/video files.
- Conversion to MP3 mono 48 kbps (~0.36 MB/min).
- MP3 files with a bitrate up to 196 kbps are copied as-is (`-c:a copy`) to avoid re-encoding already-compressed audio.
- Automatic chunking of long recordings: chunk duration is chosen based on the output audio bitrate so each chunk fits `TARGET_CHUNK_MB` and Groq's 25 MB limit.
- Tags and a comment per recording: tags show as colored chips on the card (edit via the tag icon in the card corner), the comment is shown above the transcript on the transcript page.
- Supports audio/video formats including Apple files: `.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.aac`, `.caf`, `.aif`, `.aiff`, `.wma`, `.amr`, `.3gp`, `.webm`, `.mp4`, `.mov`, `.mkv`.
- SQLite database with relative recording folder paths (`<user_id>/<recording_id>`) so the `data/` directory and database can be moved between servers.
- Sign-in with a personal Groq API key: the key is stored with the account and used for that user's transcriptions, so everyone runs on their own quota.
- Optional Telegram archive: finished recordings are copied into a private channel and pulled back on demand, so the server does not have to keep every lecture forever. Users of the app do not need a Telegram account.
- Optional Groq-powered TXT formatting (`SMART_FORMAT`) for splitting long lines.
- Light and dark theme: follows the system theme, switched manually from the upload page.

## Requirements

- Python 3.11+
- `ffmpeg` + `ffprobe`
- Debian/Ubuntu VPS (for deployment)
- A Groq API key per user: https://console.groq.com/keys

## Local run (Windows / Linux)

```bash
# Clone/copy the project
cd dictaphone

# Create .env from the example
cp .env.example .env
# edit .env

python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

pip install -r requirements.txt

# Run (no reload, so background tasks are not killed)
venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
# Linux: venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and sign in with a Groq API key (https://console.groq.com/keys).

## Test mode without Groq

For local UI testing without a real key:

```bash
MOCK_TRANSCRIPTION=true venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## User management (CLI)

```bash
# Register a user in advance (required when ALLOW_SELF_REGISTRATION=false)
python -m app.cli add-user "User name" --key gsk_...
python -m app.cli list-users
# Replace a user's key, keeping their recordings (e.g. after rotating it at Groq)
python -m app.cli rebind-key <user_id> gsk_...
python -m app.cli verify-key <user_id>
python -m app.cli delete-user <user_id> [--purge-files]
```

## Telegram archive (optional)

Finished recordings can be copied into a **private Telegram channel** that only you can
see: the audio is uploaded in parts and downloaded again when it is needed. The app talks
to Telegram from the server, so nobody who uses the app needs a Telegram account.

Setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a **private** channel (no public username, no invite links) and add the bot as
   an administrator with the "Post messages" right — bots can only join a channel as admins.
3. Post anything in the channel, then read the channel id from the bot updates:

```bash
python -m app.cli tg-discover
```

4. Put the values into `.env`:

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=-1001234567890
```

Commands:

```bash
python -m app.cli tg-status                    # configuration and counters
python -m app.cli tg-backfill                  # upload recordings that are not archived yet
python -m app.cli tg-backfill --limit 5
python -m app.cli tg-verify                    # check the archive, refresh expired file ids
python -m app.cli tg-restore <recording_id>    # download a recording back to the server
python -m app.cli tg-cleanup --dry-run         # show what the retention would drop
```

How it works:

- A finished recording is copied into the channel right after its transcript is saved.
  The user sees the transcript immediately: the upload runs in the background and a
  failed upload never turns a good transcription into an error. The app also catches up
  on missed uploads every few hours and on start.
- The audio is uploaded in parts of `TELEGRAM_CHUNK_MB` (19 MB by default), because
  Telegram hands back at most 20 MiB per `getFile`. Parts are stored with their index, so
  they can be glued back together byte for byte.
- Every message carries a caption with the recording id, the part number and the sha256 of
  the file, so the channel stays readable even if `app.db` is lost. The database keeps the
  `file_id` of every part; if Telegram expires a `file_id`, the stored message id is used
  to mint a fresh one. Downloads are checked against the recorded size and sha256.
- `AUDIO_RETENTION_DAYS` days after a recording is archived, its local audio is dropped
  and the recording is played from the archive: the audio is downloaded into the cache
  (`CACHE_DIR`, capped by `CACHE_MAX_MB`) on the first listen, and the player says so
  while it happens. Set the retention to `0` to keep every local file.
- Transcripts, TXT files and the database always stay on the server; they are only backed
  up into the channel.
- `tg-restore` marks a recording with `keep_local`, so the cleanup leaves that copy alone.
- Deleting a recording in the app deletes its messages in the channel too.
- Uploads are throttled (`TELEGRAM_SEND_INTERVAL_SECONDS`), because a channel accepts
  roughly 20 messages per minute. Archive a large backlog with `tg-backfill`.

## Users and keys

Everyone signs in with their own Groq API key and sees only their own recordings — there is no
admin role. By default any valid Groq key can register an account; set
`ALLOW_SELF_REGISTRATION=false` to accept only keys pre-registered with `add-user`.

The key is checked against the Groq API on sign-in. If Groq later rejects a stored key (revoked
or rotated), the account is flagged and new uploads fail with a clear message until the user
signs in with a valid key — previously saved transcripts stay accessible. Because the key *is*
the account identity, a brand new key creates a new account; use `rebind-key` to keep the
history of an existing one.

> **Note:** API keys are stored in the database in plain text, because they have to be replayed
> to Groq on every transcription. Keep `app.db` (and its backups) private and never publish `data/`.

## Data structure

```
data/
  ├── .cache/                 # audio pulled back from the channel (safe to delete)
  └── <user_id>/
        └── <recording_id>/
              ├── audio.mp3          # dropped after AUDIO_RETENTION_DAYS once archived
              ├── transcript.json    # always kept on the server
              └── formatted.txt      # always kept on the server
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
# edit .env: SESSION_COOKIE_SECURE=true, TRUST_PROXY_HEADERS=true (the app runs behind nginx)

sudo -u www-data python3.11 -m venv venv
sudo -u www-data venv/bin/pip install -r requirements.txt
```

4. Systemd:

```bash
sudo cp deploy/dictaphone.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dictaphone
```

5. Nginx + HTTPS:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/dictaphone
sudo ln -s /etc/nginx/sites-available/dictaphone /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# Replace example.com with your domain
sudo certbot --nginx -d example.com
```

## Updating production

The app runs from `/opt/dictaphone` as the `dictaphone` systemd service (user `www-data`),
behind nginx. An update is: get the new code onto the server, refresh dependencies if
`requirements.txt` changed, restart the service.

1. Push the release locally, then pull it on the server:

```bash
git push origin main          # or whichever branch prod tracks

ssh user@server
cd /opt/dictaphone
sudo -u www-data git pull
```

2. If `requirements.txt` changed, update the venv:

```bash
sudo -u www-data venv/bin/pip install -r requirements.txt
```

3. Restart and verify:

```bash
sudo systemctl restart dictaphone
systemctl status dictaphone
journalctl -u dictaphone -f   # watch the log if anything looks off
```

### Alternative: rsync instead of git

If the server copy is not a git checkout, sync the code from your machine. The excludes
are critical — they protect the server's `.env`, database, recordings and venv:

```bash
rsync -avz --delete \
  --exclude .git/ --exclude .env --exclude venv/ \
  --exclude 'app.db*' --exclude data/ --exclude cookies.txt \
  --exclude __pycache__/ --exclude '*.pyc' --exclude .pytest_cache/ --exclude '*.log' \
  ./ user@server:/tmp/dictaphone/

ssh user@server
sudo rsync -a --delete /tmp/dictaphone/ /opt/dictaphone/
sudo chown -R www-data:www-data /opt/dictaphone
sudo -u www-data venv/bin/pip install -r requirements.txt   # if deps changed
sudo systemctl restart dictaphone
```

Notes:

- Never overwrite `/opt/dictaphone/.env`, `app.db` or `data/` — they live only on the
  server and are gitignored. `data/` holds every recording and transcript.
- After changing `deploy/dictaphone.service`: `sudo systemctl daemon-reload` then restart.
- After changing `deploy/nginx.conf`: copy it into `sites-available`, `sudo nginx -t`,
  `sudo systemctl reload nginx`.
