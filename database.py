"""Async database infrastructure. SQL logging never includes chat or secrets."""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import Settings


class Base(DeclarativeBase):
    pass


def make_database(url: str):
    engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
    if url.startswith('sqlite'):
        @event.listens_for(engine.sync_engine, 'connect')
        def enable_foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    return engine, async_sessionmaker(engine, expire_on_commit=False)


engine, SessionLocal = make_database(Settings().database_url)
