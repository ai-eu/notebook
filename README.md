# Notebook / iPhone Audio Transcription Service

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
- **Telegram-first storage:** the audio and original uploads live in a private channel; transcripts, sentence timings and translations live in SQLite. Local files survive for `LOCAL_RETENTION_DAYS` (7 by default) after a verified archive, then only the LRU audio cache remains. Users of the app do not need a Telegram account.
- **Canonical sentence document:** the page, player and translation all read the same `sentences` artifact from SQLite. No fuzzy word matching or re-splitting on every page load.
- Optional Groq-powered TXT formatting (`SMART_FORMAT`) for very long sentences; it is applied once during processing, not on download.
- Light and dark theme: follows the system theme, switched manually from the upload page.

## Requirements

- Python 3.11+
- `ffmpeg` + `ffprobe`
- Debian/Ubuntu VPS (for deployment)
- A Groq API key per user: https://console.groq.com/keys

## Local run (Windows / Linux)

```bash
# Clone/copy the project
cd notebook

# Create .env from the example
cp .env.example .env
# edit .env

python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -r requirements.txt

# Run (no reload, so background tasks are not killed)
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
# Linux: .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and sign in with a Groq API key (https://console.groq.com/keys).

## Test mode without Groq

For local UI testing without a real key:

```bash
MOCK_TRANSCRIPTION=true .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## User management (CLI)

The commands below use the project venv explicitly, so they work without activating it.
On Windows use `.venv\Scripts\python` instead of `.venv/bin/python`.

```bash
# Register a user in advance (required when ALLOW_SELF_REGISTRATION=false)
.venv/bin/python -m app.cli add-user "User name" --key gsk_...
.venv/bin/python -m app.cli list-users
# Replace a user's key, keeping their recordings (e.g. after rotating it at Groq)
.venv/bin/python -m app.cli rebind-key <user_id> gsk_...
.venv/bin/python -m app.cli verify-key <user_id>
.venv/bin/python -m app.cli delete-user <user_id> [--purge-files]
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
.venv/bin/python -m app.cli tg-discover
```

4. Put the values into `.env`:

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=-1001234567890
```

Commands:

```bash
.venv/bin/python -m app.cli storage-status                 # configuration, counters, retention
.venv/bin/python -m app.cli storage-backfill --dry-run     # what would be imported/uploaded/evicted
.venv/bin/python -m app.cli storage-backfill               # backfill + verify + evict local files
.venv/bin/python -m app.cli storage-backfill --limit 5
.venv/bin/python -m app.cli storage-backfill --no-evict    # upload but keep local copies
.venv/bin/python -m app.cli storage-verify                 # check archive, refresh expired file ids
.venv/bin/python -m app.cli storage-restore <recording_id> # download audio back to the server
.venv/bin/python -m app.cli storage-orphans                # messages Telegram refused to delete
.venv/bin/python -m app.cli storage-orphans --retry
```

The old `tg-*` commands are still available as aliases.

How it works:

- A finished recording is copied into the channel right after its transcript is saved.
  The user sees the transcript immediately: the upload runs in the background and a
  failed upload never turns a good transcription into an error. Maintenance catches up
  every few hours.
- The audio is uploaded in parts of `TELEGRAM_CHUNK_MB` (19 MB by default), because
  Telegram hands back at most 20 MiB per `getFile`. Parts are stored with their index, so
  they can be glued back together byte for byte.
- Text artifacts (`transcript`, `sentences`, `formatted`, translations) are stored in
  SQLite and uploaded from memory. The channel is their backup, not the source of truth
  for page rendering.
- Every message carries a caption with the recording id, user id, artifact version, part
  number, size and sha256, so the channel stays readable even if `app.db` is lost. The
  database keeps the `file_id` of every part; if Telegram expires a `file_id`, the
  stored message id is used to mint a fresh one. Downloads are checked against the
  recorded size and sha256.
- `LOCAL_RETENTION_DAYS` (7 by default) days after a verified archive, local audio,
  chunks and legacy text files are deleted. The page still works from SQLite; the player
  downloads the audio into `CACHE_DIR` (capped by `CACHE_MAX_MB`) on the first listen.
  `0` evicts immediately, `-1` keeps local files forever.
- `storage-restore` marks a recording with `keep_local`, so cleanup leaves that copy alone.
- Deleting a recording deletes its messages in the channel too. Telegram's Bot API
  refuses to delete old messages (documented limit: 48 hours); such messages are moved
  to `storage-orphans` for manual cleanup instead of being silently lost.
- Uploads are throttled (`TELEGRAM_SEND_INTERVAL_SECONDS`), because a channel accepts
  roughly 20 messages per minute. Archive a large backlog with `storage-backfill`.

> **Backup warning:** Telegram is not a guaranteed backup service. Keep regular copies
> of `app.db` and export important recordings; a lost channel or bot is not recoverable
> from the application alone.

See [docs/STORAGE.md](docs/STORAGE.md) for the full storage model and recovery notes.

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
app.db                        # canonical metadata + text artifacts + translations
data/
  ├── .cache/                 # LRU audio pulled back from the channel (safe to delete)
  └── <user_id>/
        └── <recording_id>/   # only during processing and LOCAL_RETENTION_DAYS
              ├── original.tmp   # until conversion/transcription succeeds
              ├── audio.mp3      # until the verified archive + retention window
              ├── chunks/        # only during transcription, deleted afterwards
              └── legacy text    # imported into SQLite, then deleted
```

Each upload gets its own folder with the recording timestamp in the name. After eviction
the folder is removed; the recording, page and translations continue to work from
`app.db`, and the audio is fetched into `data/.cache` on demand.

## Deployment on Debian VPS

The service runs as the unprivileged `deployer` user from `/opt/apps/notebook`, with the
venv at `/opt/apps/notebook/.venv`. `uvicorn` listens on `127.0.0.1:5003` behind nginx at
`notebook.apps.yaremenko.eu`. Run steps 2-4 as `deployer`.

1. Prepare the server:

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg nginx certbot python3-certbot-nginx
```

2. Copy the project:

```bash
sudo mkdir -p /opt/apps/notebook
sudo cp -r . /opt/apps/notebook
sudo chown -R deployer:deployer /opt/apps/notebook
```

3. Configure the environment (as `deployer`):

```bash
cd /opt/apps/notebook
cp .env.example .env
# edit .env: SESSION_COOKIE_SECURE=true, TRUST_PROXY_HEADERS=true (behind nginx),
# LOCAL_RETENTION_DAYS=7, TELEGRAM_ENABLED=true

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

4. Systemd:

```bash
sudo cp deploy/notebook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now notebook
```

5. Nginx + HTTPS:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/notebook
sudo ln -s /etc/nginx/sites-available/notebook /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

sudo certbot --nginx -d notebook.apps.yaremenko.eu
```

`deploy/nginx.conf` is a template for a fresh install; Certbot adds the TLS server block.
On an existing server do not overwrite the Certbot-managed config — sync only
`server_name`, `proxy_pass`, `client_max_body_size` and the timeouts.

## Updating production

The app runs from `/opt/apps/notebook` as the `notebook` systemd service (user `deployer`,
venv `/opt/apps/notebook/.venv`), listening on `127.0.0.1:5003` behind nginx at
`notebook.apps.yaremenko.eu`. An update is: get the new code onto the server, refresh
dependencies if `requirements.txt` changed, restart the service.

1. Push the release locally, then pull it on the server:

```bash
git push origin main          # or whichever branch prod tracks

ssh deployer@217.154.102.146
cd /opt/apps/notebook
git pull
```

2. If `requirements.txt` changed, update the venv:

```bash
.venv/bin/pip install -r requirements.txt
```

3. Restart and verify:

```bash
sudo systemctl restart notebook
systemctl status notebook
journalctl -u notebook -f   # watch the log if anything looks off
```

### Alternative: rsync instead of git

If the server copy is not a git checkout, sync the code from your machine. The excludes
are critical — they protect the server's `.env`, database, recordings and venv:

```bash
rsync -avz --delete \
  --exclude .git/ --exclude .env --exclude .venv/ --exclude venv/ \
  --exclude 'app.db*' --exclude data/ --exclude cookies.txt \
  --exclude __pycache__/ --exclude '*.pyc' --exclude .pytest_cache/ --exclude '*.log' \
  ./ deployer@217.154.102.146:/tmp/notebook/

ssh deployer@217.154.102.146
sudo rsync -a --delete /tmp/notebook/ /opt/apps/notebook/
sudo chown -R deployer:deployer /opt/apps/notebook
.venv/bin/pip install -r requirements.txt   # if deps changed
sudo systemctl restart notebook
```

Notes:

- The only production environment is `/opt/apps/notebook/.venv`. Never create or update a
  second `venv/` next to it: the systemd unit runs `.venv`, so the other copy is ignored.
- Never overwrite `/opt/apps/notebook/.env`, `app.db` or `data/` — they live only on the
  server and are gitignored. `data/` holds every recording and transcript.
- After changing `deploy/notebook.service`: `sudo systemctl daemon-reload` then restart.
- After changing `deploy/nginx.conf`: copy it into `sites-available`, `sudo nginx -t`,
  `sudo systemctl reload nginx`.
