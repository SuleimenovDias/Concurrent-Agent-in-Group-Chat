"""Offline maintenance. Preview by default; stop the API before using --apply."""
import argparse
import asyncio
from config import Settings
from crud import Store
from database import SessionLocal, engine


async def main(apply: bool) -> None:
    try:
        keys = await Store(SessionLocal, Settings()).cleanup(dry_run=not apply)
        print(f'{"Deleted" if apply else "Would delete"} {len(keys)} historical version(s):')
        for task_id, version in keys:
            print(f'{task_id} v{version}')
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    asyncio.run(main(parser.parse_args().apply))
