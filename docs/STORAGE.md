# Storage model

The app uses a **Telegram-first, SQLite-canonical** model:

| Data | Canonical location | Backup / on-demand |
|---|---|---|
| `audio.mp3` | Telegram channel | local file for `LOCAL_RETENTION_DAYS`, then LRU cache |
| original upload | local working file | deleted after successful transcription |
| transcript (`transcript.json`) | `recording_artifacts` in SQLite | Telegram document |
| sentences + timings | `recording_artifacts` in SQLite | Telegram document |
| formatted TXT | `recording_artifacts` in SQLite | Telegram document |
| translations | `recording_artifacts` in SQLite | Telegram document |

The page, player and translation read SQLite. Telegram is never in the request path for
text. Audio is downloaded into `data/.cache` only when it is played after eviction.

## Why not put everything in Telegram

- `getFile` returns files up to 20 MiB unless a local Bot API server is used.
- The Bot API is rate limited; every page load would depend on the network.
- Telegram is not a backup service with an SLA. Text is tiny, so SQLite is both faster
  and safer as the canonical copy; the channel remains a recoverable backup.

## Artifact kinds

`recording_artifacts` has `(recording_id, kind)` unique:

| kind | content |
|---|---|
| `transcript` | cleaned Whisper `verbose_json` |
| `sentences` | `{"version", "duration", "sentences": [{text,start,end,paragraph,segment_ids}]}` |
| `formatted` | rendered TXT for download |
| `translation:<lang>` | `{"version", "source_sha256", "sentences": [...]}` |

`source_sha256` is the hash of the `sentences` artifact. A translation is reused only
when the hash matches, so editing or re-splitting the transcript invalidates it.

`MAX_LINE_LENGTH` is applied only when rendering `formatted` (word wrapping). It never
splits a canonical sentence, so translation and highlighting keep whole sentences.

## Recording states

- `local` — not archived yet; files are on disk.
- `partial` — some audio/artifacts are in the channel, some are not; maintenance retries.
- `tg` — audio and artifacts uploaded and verified; local files remain for the retention
  window.
- `evicted` — local files removed; page works from SQLite, audio comes from the cache.

`archive_verified_at` is set only after the audio parts are uploaded and their reported
size matches. `archived_at` is the time the full archive (audio + artifacts) completed.
Eviction requires both and `keep_local = false`.

## Retention

`LOCAL_RETENTION_DAYS`:

- `7` (default in `.env.example`) — local files survive a week after a verified archive;
- `0` — evict immediately;
- `-1` — never evict (local mode).

`AUDIO_RETENTION_DAYS` is a deprecated fallback used when the new variable is absent.

Eviction deletes `audio.mp3`, `original.tmp`, `chunks/`, `tg_chunks/`, legacy
`transcript.json` / `formatted*.txt`, and then removes the empty folder. It never deletes
`data/.cache`; that cache is LRU-managed by `CACHE_MAX_MB`.

If Telegram is not configured, nothing is evicted: the app falls back to local storage.

## Deletion and the 48-hour limit

Before a `Recording` row is deleted, its message ids are copied to `remote_cleanup`.
The app attempts deletion immediately; failures are retried by maintenance. Telegram's
Bot API documents that messages can only be deleted while they are fresh (48 hours), so
older refused messages are marked `manual` and listed by:

```bash
.venv/bin/python -m app.cli storage-orphans
.venv/bin/python -m app.cli storage-orphans --retry
```

Manual deletion must be done by an administrator in Telegram. This is a Bot API
limitation, not an application bug.

## Backfill and recovery

```bash
.venv/bin/python -m app.cli storage-backfill --dry-run
.venv/bin/python -m app.cli storage-backfill --no-evict
.venv/bin/python -m app.cli storage-backfill
.venv/bin/python -m app.cli storage-verify
```

Backfill imports legacy text files into SQLite, builds `sentences` (using the old
timing alignment for old `formatted.txt`), uploads audio and text, verifies sizes, and
then evicts local files unless `--no-evict` is set. It is idempotent.

On restart, `recover_interrupted_jobs()` requeues `pending`/`processing` recordings when
the source files still exist, resumes from `audio.mp3` when conversion already happened,
and marks the recording as `error` after `MAX_JOB_ATTEMPTS`.

## Operational limits

- One uvicorn worker. Progress, job semaphores and translation locks are in-memory.
  Multiple workers would duplicate maintenance and split progress state.
- Telegram channel is shared by all users; captions include `user_id` (`uid`) for
  recovery. Keep the channel private.
- Keep regular backups of `app.db`. The channel is a backup, not a guarantee.
