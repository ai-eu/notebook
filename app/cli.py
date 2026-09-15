import argparse
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, delete as sa_delete
from app.database import engine, Base, AsyncSessionLocal
from app.models import User
from app.auth import generate_key, hash_key, get_key_lookup_hash


async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def create_key(label: str, is_admin: bool = False):
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        key = generate_key()
        user = User(
            key_hash=hash_key(key),
            key_lookup_hash=get_key_lookup_hash(key),
            label=label,
            is_admin=is_admin,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.commit()
        print(f"Created key: {key}")
        print(f"Label: {label}")
        print(f"Admin: {is_admin}")


async def list_keys():
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).order_by(User.created_at))
        for u in result.scalars():
            print(f"{u.id}\t{u.label or '-'}\t{u.is_admin}\t{u.created_at}\t{u.last_seen_at}")


async def revoke_key(user_id: int):
    await ensure_tables()
    async with AsyncSessionLocal() as db:
        await db.execute(sa_delete(User).where(User.id == user_id))
        await db.commit()
        print(f"User {user_id} revoked.")


def main():
    parser = argparse.ArgumentParser(description="Access key management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-key", help="Create a new access key")
    p_create.add_argument("label")
    p_create.add_argument("--admin", action="store_true")

    sub.add_parser("list-keys", help="List access keys")

    p_revoke = sub.add_parser("revoke-key", help="Revoke an access key")
    p_revoke.add_argument("user_id", type=int)

    args = parser.parse_args()
    if args.cmd == "create-key":
        asyncio.run(create_key(args.label, args.admin))
    elif args.cmd == "list-keys":
        asyncio.run(list_keys())
    elif args.cmd == "revoke-key":
        asyncio.run(revoke_key(args.user_id))


if __name__ == "__main__":
    main()
