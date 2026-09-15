import argparse
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import engine, Base, AsyncSessionLocal
from app.models import User
from app.auth import delete_user_sessions
from app.services.groq import is_groq_key_format, mask_key, validate_key
from app.utils import user_data_dir, safe_delete


async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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


def main():
    parser = argparse.ArgumentParser(description="User management")
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


if __name__ == "__main__":
    main()
