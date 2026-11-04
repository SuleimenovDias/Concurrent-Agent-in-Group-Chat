"""Read-only database connectivity check, safe to import during pytest collection."""
import asyncio
from sqlalchemy import text
from database import engine


async def main() -> None:
    try:
        async with engine.connect() as connection:
            await connection.execute(text('SELECT 1'))
        print('Database connected.')
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
