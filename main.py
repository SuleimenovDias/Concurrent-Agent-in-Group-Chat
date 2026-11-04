"""REST interface for the chat simulator. Run one Uvicorn worker only."""
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from config import Settings
from crud import DomainError, Store, is_bot_mentioned
from database import SessionLocal, engine
from executor import OpenAIExecutor, OpenAIGateway
from models import ChatMessage, DependencyDecision, TaskRecord, TaskVersion
from router import OpenAIRouter
from scheduler import Scheduler
from demo_mock import DemoRouter, DemoExecutor
from schemas import DecisionRequest, DecisionResponse, EventPage, MessageAccepted, MessageRequest, MessageResponse, TaskResponse, VersionResponse

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, sessions=None, db_engine=None, router=None, executor=None) -> FastAPI:
    settings = settings or Settings()
    sessions, db_engine = sessions or SessionLocal, db_engine or engine
    gateway = OpenAIGateway(settings)
    store = Store(sessions, settings)
    scheduler = Scheduler(store, router or DemoRouter(OpenAIRouter(gateway, settings)),
                          executor or DemoExecutor(OpenAIExecutor(gateway, settings), settings), settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scheduler.start()  # Schema migrations must have been run explicitly.
        try:
            yield
        finally:
            await scheduler.stop()
            await gateway.close()
            await db_engine.dispose()

    app = FastAPI(title='Castle Concurrent Group-Chat Agent', version='1.0.0', lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
    app.state.store, app.state.scheduler = store, scheduler

    @app.exception_handler(DomainError)
    async def domain_error(_, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})

    @app.get('/demo-config')
    async def demo_config():
        return {'mock_available': True, 'live_available': bool(settings.openai_api_key),
                'live_delay_seconds': settings.demo_execution_delay_seconds}

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/db-health')
    async def database_health():
        try:
            async with sessions() as s:
                await s.execute(text('SELECT 1'))
            return {'status': 'connected'}
        except Exception as exc:
            log.error('Database health failed: %s', type(exc).__name__)
            return JSONResponse(status_code=503, content={'status': 'unavailable'})

    @app.post('/messages', status_code=202, response_model=MessageAccepted)
    async def receive_message(message: MessageRequest):
        saved = await store.submit(message)
        scheduler.notify()
        return MessageAccepted(message_id=saved.message_id, status='accepted' if is_bot_mentioned(saved.content) else 'stored_no_action')

    @app.get('/messages/{message_id}', response_model=MessageResponse)
    async def get_message(message_id: str):
        async with sessions() as s:
            message = await s.get(ChatMessage, message_id)
            if message is None:
                raise DomainError('Message not found', 404)
            return MessageResponse.model_validate(message)

    @app.get('/tasks/{task_id}', response_model=TaskResponse)
    async def get_task(task_id: str):
        return await store.task(task_id)

    @app.get('/tasks/{task_id}/versions', response_model=list[VersionResponse])
    async def versions(task_id: str, after_version: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        await store.task(task_id)
        async with sessions() as s:
            return list((await s.scalars(select(TaskVersion).where(TaskVersion.task_id == task_id,
                TaskVersion.version > after_version).order_by(TaskVersion.version).limit(limit))).all())

    @app.get('/tasks/{task_id}/versions/{version}', response_model=TaskResponse)
    async def get_version(task_id: str, version: int):
        return await store.task(task_id, version)

    @app.get('/chats/{chat_id}/messages', response_model=list[MessageResponse])
    async def messages(chat_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        async with sessions() as s:
            return list((await s.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id,
                ChatMessage.sequence > after).order_by(ChatMessage.sequence).limit(limit))).all())

    @app.get('/chats/{chat_id}/tasks', response_model=list[TaskResponse])
    async def tasks(chat_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        async with sessions() as s:
            ids = list((await s.scalars(select(TaskRecord.task_id).where(TaskRecord.chat_id == chat_id)
                .order_by(TaskRecord.task_id).offset(offset).limit(limit))).all())
        return [await store.task(task_id) for task_id in ids]

    @app.get('/chats/{chat_id}/events', response_model=EventPage)
    async def events(chat_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return await store.events(chat_id, after, limit)

    @app.get('/chats/{chat_id}/decisions', response_model=list[DecisionResponse])
    async def decisions(chat_id: str):
        async with sessions() as s:
            return list((await s.scalars(select(DependencyDecision).join(TaskRecord, TaskRecord.task_id == DependencyDecision.task_id)
                .where(TaskRecord.chat_id == chat_id, DependencyDecision.status == 'pending')
                .order_by(DependencyDecision.created_at).limit(200))).all())

    @app.post('/tasks/{task_id}/cancel', response_model=TaskResponse)
    async def cancel(task_id: str):
        response = await store.cancel(task_id)
        scheduler.notify()
        return response

    @app.post('/dependency-decisions/{decision_id}/resolve', response_model=DecisionResponse)
    async def resolve(decision_id: str, request: DecisionRequest):
        result = await store.decide(decision_id, request.user_id, request.choice)
        scheduler.notify()
        return result

    return app


app = create_app()
