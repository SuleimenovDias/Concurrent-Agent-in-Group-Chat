"""Single-process asyncio orchestration; the database is the source of readiness."""
import asyncio
import logging
from dataclasses import dataclass

from config import Settings
from crud import DomainError, Key, Store
from executor import Executor, safe_error
from router import Router
from schemas import RouteDecision

log = logging.getLogger(__name__)


@dataclass
class Worker:
    task: asyncio.Task | None = None
    token: str | None = None


class Scheduler:
    def __init__(self, store: Store, router: Router, executor: Executor, settings: Settings):
        self.store, self.router, self.executor, self.settings = store, router, executor, settings
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_agents)
        self.route_semaphore = asyncio.Semaphore(settings.max_concurrent_routes)
        self.workers: dict[Key, Worker] = {}
        self.routers: dict[str, asyncio.Task] = {}
        self.wake = asyncio.Event()
        self.stopping = False
        self.runner: asyncio.Task | None = None

    async def start(self) -> None:
        await self.store.recover()
        self.runner = asyncio.create_task(self._loop(), name='castle-scheduler')

    def notify(self) -> None:
        self.wake.set()

    async def _loop(self) -> None:
        while not self.stopping:
            self.wake.clear()
            try:
                await self.tick()
            except Exception as exc:
                # A temporary DB failure must not kill scheduling. Avoid exception bodies containing data.
                log.error('Scheduler reconciliation failed: %s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), self.settings.scheduler_interval_seconds)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        states = await self.store.work_state()
        # A completion-write failure can leave a running row after its asyncio
        # task exits. Requeue that orphan instead of requiring a process restart.
        for key, (status, token) in states.items():
            if status == 'running' and token and key not in self.workers:
                await self.store.release(key, token)
        for key, worker in list(self.workers.items()):
            status, token = states.get(key, ('terminal', None))
            if status not in {'queued', 'running'} or (worker.token is not None and token != worker.token):
                worker.task.cancel()
        for key, (status, _) in states.items():
            if status == 'queued' and key not in self.workers:
                self.schedule_worker(key)
        for chat_id in await self.store.pending_chats():
            if chat_id not in self.routers:
                task = asyncio.create_task(self._route_chat(chat_id), name=f'route:{chat_id}')
                self.routers[chat_id] = task
                task.add_done_callback(lambda done, chat=chat_id: self._route_done(chat, done))

    def schedule_worker(self, key: Key) -> bool:
        if self.stopping or key in self.workers:
            return False
        worker = Worker()
        self.workers[key] = worker
        worker.task = asyncio.create_task(self._execute(key, worker), name=f'execute:{key[0]}:{key[1]}')
        worker.task.add_done_callback(lambda done: self._worker_done(key, worker, done))
        return True

    def _worker_done(self, key: Key, worker: Worker, task: asyncio.Task) -> None:
        if self.workers.get(key) is worker:
            self.workers.pop(key)
        self._check_task(task)
        self.notify()

    def _route_done(self, chat_id: str, task: asyncio.Task) -> None:
        if self.routers.get(chat_id) is task:
            self.routers.pop(chat_id)
        self._check_task(task)
        self.notify()

    @staticmethod
    def _check_task(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error('Background operation failed: %s', type(exc).__name__)

    async def _execute(self, key: Key, worker: Worker) -> None:
        try:
            async with self.semaphore:
                inputs = await self.store.claim(key)
                if inputs is None:
                    return
                worker.token = inputs['run_token']
                try:
                    async with asyncio.timeout(self.settings.task_timeout_seconds):
                        answer = await self.executor.execute(inputs)
                    if not isinstance(answer, str) or not answer.strip():
                        raise ValueError('Empty execution result')
                except Exception as exc:
                    log.warning('Task %s v%d failed: %s', *key, type(exc).__name__)
                    await self.store.finish(key, worker.token, error=safe_error(exc))
                else:
                    await self.store.finish(key, worker.token, result=answer)
        except asyncio.CancelledError:
            if worker.token:
                await self.store.release(key, worker.token)
            raise

    async def _route_chat(self, chat_id: str) -> None:
        # FIFO routing within a chat gives immediate follow-ups a consistent task context.
        # Execution is independently concurrent; no chat-level FIFO answer publication.
        while not self.stopping:
            message = await self.store.claim_message(chat_id)
            if message is None:
                return
            try:
                context = await self.store.routing_context(message)
                async with self.route_semaphore:
                    async with asyncio.timeout(self.settings.task_timeout_seconds):
                        decision = await self.router.route(context)
                try:
                    await self.store.apply_route(message.message_id, decision)
                except DomainError:
                    await self.store.apply_route(message.message_id, RouteDecision(action='clarification', target_task_ids=[],
                        reason='That reference cannot be used. Please identify a task in this chat; only its owner can revise it.'))
            except asyncio.CancelledError:
                raise  # Startup recovery returns routing messages to pending.
            except Exception as exc:
                log.warning('Routing failed: %s', type(exc).__name__)
                await self.store.route_failed(message.message_id, safe_error(exc))
            self.notify()

    async def stop(self) -> None:
        self.stopping = True
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        jobs = [w.task for w in self.workers.values()] + list(self.routers.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
