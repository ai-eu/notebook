import argparse
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select
from app.config import settings
from app.database import init_db, AsyncSessionLocal
from app.models import User
from app.auth import delete_user_sessions
from app.services.groq import is_groq_key_format, mask_key, validate_key
from app.services.storage import archive, telegram
from app.utils import user_data_dir, safe_delete


async def ensure_tables():
    await init_db()


async def add_user(label: str, key: str, verify: bool = True):
    await ensure_tables()
    key = key.strip()
    if not is_groq_key_format(key):
        print("That does not look like a Groq API key (expected gsk_...).")
        return
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.groq_key == key))
        if existing.scalar_one_or_none():
            print("This key is already registered.")
            return
        user = User(groq_key=key, label=label, last_verified_at=datetime.now(timezone.utc))
        db.add(user)
        await db.commit()
        await db.refresh(user)
        print(f"Created user {user.id}: {label} ({mask_key(key)})")
        if verify:
            await _check_and_store(db, user, await validate_key(key))


async def list_users():
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).order_by(User.created_at))
        for u in result.scalars():
            print(f"{u.id}\t{u.label or '-'}\t{mask_key(u.groq_key)}\tvalid={u.key_valid}\t{u.created_at}\t{u.last_seen_at}")


async def rebind_key(user_id: int, new_key: str):
    await ensure_tables()
    new_key = new_key.strip()
    if not is_groq_key_format(new_key):
        print("That does not look like a Groq API key (expected gsk_...).")
        return
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            print(f"User {user_id} not found.")
            return
        taken = await db.execute(select(User).where(User.groq_key == new_key, User.id != user_id))
        if taken.scalar_one_or_none():
            print("This key is already registered to another user.")
            return
        user.groq_key = new_key
        user.key_valid = True
        user.last_verified_at = datetime.now(timezone.utc)
        if user.label is None or user.label.startswith("gsk_"):
            # Auto-generated labels mirror the key, so refresh them on rotation.
            user.label = mask_key(new_key)
        await db.commit()
        # Old sessions must not survive a key rotation.
        await delete_user_sessions(db, user_id)
        print(f"User {user_id} now uses {mask_key(new_key)}; previous sessions revoked.")


async def delete_user(user_id: int, purge_files: bool = False):
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            print(f"User {user_id} not found.")
            return
        await db.delete(user)
        await db.commit()
    folder = user_data_dir(user_id)
    if purge_files:
        safe_delete(folder)
        print(f"Deleted user {user_id} and the files in {folder}.")
    else:
        print(f"Deleted user {user_id}. Files in {folder} were kept (use --purge-files to remove them).")


async def verify_key(user_id: int):
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            print(f"User {user_id} not found.")
            return
        await _check_and_store(db, user, await validate_key(user.groq_key))


async def _check_and_store(db, user: User, status: str) -> None:
    if status == "invalid":
        user.key_valid = False
    elif status in ("valid", "throttled"):
        user.key_valid = True
        user.last_verified_at = datetime.now(timezone.utc)
    await db.commit()
    print(f"User {user.id} ({user.label or '-'}): Groq key check — {status}")


def _human_mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


async def tg_discover():
    await ensure_tables()
    if not settings.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (get one from @BotFather).")
        return
    try:
        chats = await telegram.discover_chats()
    except telegram.TelegramError as exc:
        print(f"Telegram error: {exc}")
        return
    if not chats:
        print("The bot has not seen any chat yet.")
        print("Create the private channel, add the bot as an administrator, post anything in the")
        print("channel and run this command again.")
        return
    print("Chats seen by the bot (channel ids start with -100):")
    for chat in chats:
        print(f"  {chat['id']}\t{chat['type']}\t{chat['title'] or '-'}")


async def tg_status():
    await ensure_tables()
    print(f"TELEGRAM_ENABLED: {settings.telegram_enabled}")
    print(f"TELEGRAM_BOT_TOKEN: {telegram.mask_token(settings.telegram_bot_token)}")
    print(f"TELEGRAM_CHAT_ID: {settings.telegram_chat_id or '-'}")
    if settings.telegram_bot_token:
        try:
            me = await telegram.get_me()
            print(f"Bot: @{me.get('username')} ({me.get('id')})")
        except telegram.TelegramError as exc:
            print(f"Bot check failed: {exc}")
    summary = await archive.status_summary()
    states = ", ".join(f"{state}={count}" for state, count in sorted(summary["states"].items()))
    print(f"Recordings: {summary['recordings']} ({states or 'none'})")
    print(f"Files in the channel: {summary['files']} ({_human_mb(summary['bytes_remote'])})")
    if telegram.is_configured():
        pending = await archive.pending_recordings()
        print(f"Waiting to be archived: {len(pending)}")


async def tg_backfill(limit: int | None = None):
    await ensure_tables()
    if not telegram.is_configured():
        print("Telegram is not configured: set TELEGRAM_ENABLED=true, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    pending = await archive.pending_recordings(limit)
    if not pending:
        print("Nothing to archive.")
        return
    print(f"Archiving {len(pending)} recording(s)...")
    failed = 0
    for recording_id in pending:
        try:
            result = await archive.archive_recording(recording_id)
        except (telegram.TelegramError, OSError, ValueError) as exc:
            failed += 1
            print(f"{recording_id}: failed — {exc}")
            continue
        print(f"{recording_id}: {result['uploaded']} of {result['parts'] + 1} file(s) uploaded")
    print(f"Done. Failed: {failed}")


async def tg_verify(recording_id: str | None = None, repair: bool = True):
    await ensure_tables()
    if not telegram.is_configured():
        print("Telegram is not configured: set TELEGRAM_ENABLED=true, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    targets = [recording_id] if recording_id else await archive.archived_recordings()
    if not targets:
        print("No archived recordings.")
        return
    problems: list[str] = []
    for target in targets:
        problems.extend(await archive.verify_recording(target, repair=repair))
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return
    print(f"All files of {len(targets)} recording(s) are readable.")


async def tg_restore(recording_id: str, force: bool = False):
    await ensure_tables()
    if not telegram.is_configured():
        print("Telegram is not configured: set TELEGRAM_ENABLED=true, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    try:
        path, downloaded = await archive.restore_recording(recording_id, force=force)
    except (telegram.TelegramError, OSError, ValueError) as exc:
        print(f"Restore failed: {exc}")
        return
    if downloaded:
        print(f"Restored {recording_id} to {path}")
    else:
        print(f"Local copy already exists: {path} (use --force to download it again)")


def main():
    parser = argparse.ArgumentParser(description="User management and Telegram archive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add-user", help="Register a user with their Groq API key")
    p_add.add_argument("label")
    p_add.add_argument("--key", required=True, help="Groq API key (gsk_...)")
    p_add.add_argument("--no-verify", action="store_true", help="Skip the Groq API check")

    sub.add_parser("list-users", help="List users")

    p_rebind = sub.add_parser("rebind-key", help="Replace a user's Groq key, keeping their recordings")
    p_rebind.add_argument("user_id", type=int)
    p_rebind.add_argument("new_key")

    p_delete = sub.add_parser("delete-user", help="Delete a user and their recordings")
    p_delete.add_argument("user_id", type=int)
    p_delete.add_argument("--purge-files", action="store_true", help="Also delete the user's files")

    p_verify = sub.add_parser("verify-key", help="Check a user's Groq key against the API")
    p_verify.add_argument("user_id", type=int)

    sub.add_parser("tg-discover", help="List the chats the bot has seen, to find the channel id")

    sub.add_parser("tg-status", help="Show the Telegram archive configuration and counters")

    p_backfill = sub.add_parser("tg-backfill", help="Upload recordings that are not archived yet")
    p_backfill.add_argument("--limit", type=int, default=None, help="Stop after this many recordings")

    p_tg_verify = sub.add_parser("tg-verify", help="Check the archived files and refresh expired file ids")
    p_tg_verify.add_argument("recording_id", nargs="?", default=None)
    p_tg_verify.add_argument("--no-repair", action="store_true", help="Only report problems")

    p_restore = sub.add_parser("tg-restore", help="Download a recording back from the channel")
    p_restore.add_argument("recording_id")
    p_restore.add_argument("--force", action="store_true", help="Overwrite an existing local copy")

    args = parser.parse_args()
    if args.cmd == "add-user":
        asyncio.run(add_user(args.label, args.key, verify=not args.no_verify))
    elif args.cmd == "list-users":
        asyncio.run(list_users())
    elif args.cmd == "rebind-key":
        asyncio.run(rebind_key(args.user_id, args.new_key))
    elif args.cmd == "delete-user":
        asyncio.run(delete_user(args.user_id, purge_files=args.purge_files))
    elif args.cmd == "verify-key":
        asyncio.run(verify_key(args.user_id))
    elif args.cmd == "tg-discover":
        asyncio.run(tg_discover())
    elif args.cmd == "tg-status":
        asyncio.run(tg_status())
    elif args.cmd == "tg-backfill":
        asyncio.run(tg_backfill(args.limit))
    elif args.cmd == "tg-verify":
        asyncio.run(tg_verify(args.recording_id, repair=not args.no_repair))
    elif args.cmd == "tg-restore":
        asyncio.run(tg_restore(args.recording_id, force=args.force))


if __name__ == "__main__":
    main()
