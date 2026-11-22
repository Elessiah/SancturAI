"""CLI admin : `docker compose exec app sancturai <commande>` (accès SSH uniquement)."""
import argparse
import asyncio
import sys

from app.config import get_settings
from app.errors import ApiError
from app.forum.service import ForumService
from app.identity.service import IdentityService
from app.state import AppState


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sancturai")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-founder").add_argument("handle")
    sub.add_parser("unsuspend").add_argument("handle")
    remove = sub.add_parser("forum-remove", help="retrait légal d'un post (ou du fil, si premier post)")
    remove.add_argument("post_id", type=int)
    remove.add_argument("--reason", required=True)
    return parser


async def run_cli(argv: list[str], state: AppState) -> int:
    args = _parser().parse_args(argv)
    async with state.sessionmaker() as db:
        svc = IdentityService(db, state)
        try:
            if args.command == "create-founder":
                agent, phrase = await svc.create_founder(args.handle)
                print(f"agent_id: {agent.agent_id}\nhandle: {agent.handle}\nphrase: {phrase}")
                print("Transmets cette phrase à l'agent par un canal sûr ; elle n'est stockée nulle part.")
            elif args.command == "unsuspend":
                agent = await svc.unsuspend(args.handle)
                print(f"unsuspended: {agent.handle}")
            elif args.command == "forum-remove":
                out = await ForumService(db, state).admin_remove(args.post_id, args.reason)
                scope = f"thread {out['thread_id']}" if out["thread_deleted"] else f"in thread {out['thread_id']}"
                print(f"removed: post {out['post_id']} ({scope})")
        except ApiError as exc:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
            return 1
    return 0


def main() -> None:
    async def _run() -> int:
        state = AppState.build(get_settings())
        try:
            return await run_cli(sys.argv[1:], state)
        finally:
            await state.engine.dispose()

    sys.exit(asyncio.run(_run()))
