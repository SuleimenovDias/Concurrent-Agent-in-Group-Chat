"""Read-only configuration/DB checks; --models adds non-generation model metadata requests."""
import argparse
import asyncio
import os

from dotenv import dotenv_values
from openai import APIStatusError
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from config import ROOT, Settings
from database import make_database
from executor import OpenAIGateway, safe_error


def report_configuration(settings: Settings) -> None:
    values = dotenv_values(ROOT / '.env')
    for name in ['DATABASE_URL', 'OPENAI_API_KEY', 'ROUTER_MODEL', 'EXECUTOR_MODEL', 'LLM_PROVIDER']:
        if name in os.environ:
            print(f'{name}: exported override present; differs from .env = {os.environ[name] != values.get(name)}')
    url = make_url(settings.database_url)
    print('Effective database username:', url.username)
    print('Effective database name:', url.database)
    print('Router model:', settings.router_model)
    print('Executor model:', settings.executor_model)


async def main(check_models: bool) -> None:
    settings = Settings()
    report_configuration(settings)
    engine, _ = make_database(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text('SELECT 1'))
            print('Database connection: PASS')
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
            print('Tables:', ', '.join(tables))
    except Exception as exc:
        original = getattr(exc, 'orig', None)
        print('Database connection failed:', type(exc).__name__, 'SQLSTATE:', getattr(original, 'sqlstate', None))
    finally:
        await engine.dispose()
    if check_models:
        gateway = OpenAIGateway(settings)
        try:
            for model in dict.fromkeys([settings.router_model, settings.executor_model]):
                try:
                    await gateway._client().models.retrieve(model)
                    print(f'{model}: metadata accessible (generation/billing not tested)')
                except APIStatusError as exc:
                    print(f'{model}: {safe_error(exc)}')
        except Exception as exc:
            print('Model check failed:', safe_error(exc))
        finally:
            await gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', action='store_true')
    asyncio.run(main(parser.parse_args().models))
