import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import Settings
from crud import Store
from database import Base, make_database
import models  # noqa: F401
from scheduler import Scheduler
from schemas import MessageRequest, RouteDecision


@pytest.fixture
async def db(tmp_path):
    url = os.getenv('TEST_DATABASE_URL')
    if url:
        parsed = make_url(url)
        if not (parsed.database or '').startswith('castle_test_'):
            raise RuntimeError('TEST_DATABASE_URL must name an isolated castle_test_* database')
        if url == Settings().database_url:
            raise RuntimeError('Tests must never use the configured development database')
        schema = 'test_' + uuid4().hex
        admin = create_async_engine(url, hide_parameters=True)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA {schema}'))
        engine = create_async_engine(url, hide_parameters=True, connect_args={'server_settings': {'search_path': schema}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
    else:
        engine, sessions = make_database(f'sqlite+aiosqlite:///{tmp_path}/test.db')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(_env_file=None, database_url=str(engine.url), openai_api_key=None,
                        scheduler_interval_seconds=0.01, retry_base_seconds=0, task_timeout_seconds=3)
    yield engine, sessions, settings
    await engine.dispose()
    if url:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()


@pytest.fixture
async def store(db):
    return Store(db[1], db[2])


class NewRouter:
    calls = 0
    async def route(self, context):
        self.calls += 1
        return RouteDecision(action='new_task', target_task_ids=[], reason='Independent request')


@pytest.fixture
async def schedulers(store, db):
    running = []
    async def start(executor, router=None, **changes):
        settings = db[2].model_copy(update=changes)
        scheduler = Scheduler(store, router or NewRouter(), executor, settings)
        await scheduler.start()
        running.append(scheduler)
        return scheduler
    yield start
    for scheduler in reversed(running):
        await scheduler.stop()


async def new_task(store, content='task', user='alice', chat='chat', refs=()):
    message = await store.submit(MessageRequest(chat_id=chat, user_id=user, content='@CastleBot ' + content))
    key = await store.apply_route(message.message_id, RouteDecision(
        action='dependent_task' if refs else 'new_task', target_task_ids=list(refs), reason='Test'))
    return key


async def revise(store, task_id, content='updated', user='alice', chat='chat'):
    message = await store.submit(MessageRequest(chat_id=chat, user_id=user, content='@CastleBot ' + content))
    return await store.apply_route(message.message_id, RouteDecision(action='follow_up', target_task_ids=[task_id], reason='Test'))


async def complete(store, key, result='answer'):
    inputs = await store.claim(key)
    assert inputs is not None
    assert await store.finish(key, inputs['run_token'], result=result)
    return inputs
