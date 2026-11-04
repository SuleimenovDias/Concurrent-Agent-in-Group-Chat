"""Transactional orchestration. One Store per application/process is required.

The short mutation lock serializes read/check/write invariants (including SQLite
in tests). PostgreSQL chat-row locks also serialize publication counters. No LLM
request or dependency wait ever happens inside these transactions.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import Settings
from models import ACTIVE, ChatEvent, ChatMessage, ChatState, DependencyDecision, TaskDependency, TaskRecord, TaskVersion, utcnow
from schemas import DecisionResponse, DependencyResponse, EventPage, EventResponse, MessageRequest, RouteDecision, TaskResponse, VersionResponse

Key = tuple[str, int]


class DomainError(Exception):
    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def is_bot_mentioned(content: str) -> bool:
    return bool(re.search(r'(?<!\w)@CastleBot\b', content, re.IGNORECASE))


class Store:
    def __init__(self, sessions: async_sessionmaker, settings: Settings):
        self.sessions = sessions
        self.settings = settings
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            async with self.sessions.begin() as session:
                yield session

    async def _chat(self, s: AsyncSession, chat_id: str) -> ChatState:
        state = await s.scalar(select(ChatState).where(ChatState.chat_id == chat_id).with_for_update())
        if state is None:
            state = ChatState(chat_id=chat_id, last_sequence=0, last_message_sequence=0)
            s.add(state)
            await s.flush()
        return state

    async def _event(self, s: AsyncSession, chat_id: str, kind: str, payload: dict,
                     task: TaskVersion | None = None, message_id: str | None = None,
                     dedupe: str | None = None) -> None:
        dedupe = dedupe or str(uuid4())
        if await s.scalar(select(ChatEvent.event_id).where(ChatEvent.dedupe_key == dedupe)):
            return
        chat = await self._chat(s, chat_id)
        chat.last_sequence += 1
        s.add(ChatEvent(event_id=str(uuid4()), chat_id=chat_id, sequence=chat.last_sequence,
                        type=kind, task_id=task.task_id if task else None,
                        version=task.version if task else None,
                        message_id=message_id or (task.message_id if task else None),
                        payload=payload, dedupe_key=dedupe))
        await s.flush()

    async def _status(self, s: AsyncSession, v: TaskVersion, status: str, error: str | None = None) -> None:
        if v.status == status and v.error == error:
            return
        v.status, v.error = status, error
        if status != 'running':
            v.run_token = None
        if status in {'completed', 'failed', 'cancelled'}:
            v.completed_at = v.completed_at or utcnow()
        elif v.result is None:
            v.completed_at = None
        task = await s.get(TaskRecord, v.task_id)
        if task.version == v.version:
            task.content, task.status, task.result = v.content, status, v.result
        await self._event(s, task.chat_id, 'failure_notification' if status == 'failed' else 'task_status',
                          {'status': status, 'error': error, 'current': task.version == v.version}, v)

    async def _edges(self, s: AsyncSession, key: Key) -> list[TaskDependency]:
        return list((await s.scalars(select(TaskDependency).where(
            TaskDependency.task_id == key[0], TaskDependency.version == key[1]))).all())

    async def _pending(self, s: AsyncSession, key: Key) -> list[DependencyDecision]:
        return list((await s.scalars(select(DependencyDecision).where(
            DependencyDecision.task_id == key[0], DependencyDecision.version == key[1],
            DependencyDecision.status == 'pending'))).all())

    async def _validate_edges(self, s: AsyncSession, task: TaskRecord, refs: list[Key]) -> None:
        if len({key[0] for key in refs}) != len(refs):
            raise DomainError('Duplicate prerequisites', 422)
        # Conservative logical-task DAG: even cross-version self-dependencies are rejected.
        graph: dict[str, set[str]] = {}
        for edge in (await s.scalars(select(TaskDependency))).all():
            graph.setdefault(edge.task_id, set()).add(edge.prerequisite_task_id)
        graph[task.task_id] = {key[0] for key in refs}
        for task_id, version in refs:
            parent = await s.get(TaskRecord, task_id)
            if parent is None or parent.chat_id != task.chat_id or await s.get(TaskVersion, (task_id, version)) is None:
                raise DomainError('Prerequisite is not available in this chat', 422)
            todo, seen = [task_id], set()
            while todo:
                node = todo.pop()
                if node == task.task_id:
                    raise DomainError('Dependency cycle rejected', 422)
                if node not in seen:
                    seen.add(node)
                    todo.extend(graph.get(node, ()))

    async def _add_edges(self, s: AsyncSession, task: TaskRecord, v: TaskVersion, refs: list[Key]) -> None:
        await self._validate_edges(s, task, refs)
        for parent_id, parent_version in refs:
            s.add(TaskDependency(task_id=v.task_id, version=v.version,
                                 prerequisite_task_id=parent_id, prerequisite_version=parent_version))
        await s.flush()

    async def _settle(self, s: AsyncSession) -> None:
        """Resolve readiness/failure and retain the transitive closure of needed versions."""
        await s.flush()
        tasks = {t.task_id: t for t in (await s.scalars(select(TaskRecord))).all()}
        versions = {(v.task_id, v.version): v for v in (await s.scalars(select(TaskVersion))).all()}
        edges: dict[Key, list[Key]] = {}
        for e in (await s.scalars(select(TaskDependency))).all():
            edges.setdefault((e.task_id, e.version), []).append((e.prerequisite_task_id, e.prerequisite_version))
        decisions = list((await s.scalars(select(DependencyDecision).where(DependencyDecision.status == 'pending'))).all())
        pending = {(d.task_id, d.version) for d in decisions}
        needed = {k for k, v in versions.items() if tasks[k[0]].version == k[1] and v.status in ACTIVE}
        for d in decisions:
            needed.update({(d.prerequisite_task_id, d.original_version), (d.prerequisite_task_id, d.updated_version)})
        todo = list(needed)
        while todo:
            for parent in edges.get(todo.pop(), []):
                if parent not in needed:
                    needed.add(parent)
                    todo.append(parent)
        # Propagate terminal failures through arbitrary depth in one transaction.
        changed = True
        while changed:
            changed = False
            for key, v in versions.items():
                if v.status not in ACTIVE:
                    continue
                old_status = v.status
                if key not in pending and v.result is not None:
                    # A completed answer paused for a decision remains a
                    # completed historical snapshot after the dependent revises.
                    await self._status(s, v, 'completed')
                elif tasks[key[0]].version != key[1] and key not in needed:
                    await self._status(s, v, 'cancelled', 'Superseded and no longer required')
                elif key in pending:
                    await self._status(s, v, 'waiting_for_decision')
                else:
                    parents = [versions[p] for p in edges.get(key, [])]
                    failed = next((p for p in parents if p.status in {'failed', 'cancelled'}), None)
                    if failed:
                        await self._status(s, v, 'failed', f'Prerequisite {failed.task_id} v{failed.version} {failed.status}')
                    elif any(p.status != 'completed' for p in parents):
                        await self._status(s, v, 'waiting_for_dependencies')
                    elif v.status != 'running':
                        await self._status(s, v, 'queued')
                changed |= old_status != v.status
        await s.flush()

    async def submit(self, request: MessageRequest) -> ChatMessage:
        async with self.transaction() as s:
            chat = await self._chat(s, request.chat_id)
            chat.last_message_sequence += 1
            message = ChatMessage(message_id=str(uuid4()), **request.model_dump(),
                                  sequence=chat.last_message_sequence,
                                  routing_status='pending' if is_bot_mentioned(request.content) else 'stored_no_action')
            s.add(message)
            await s.flush()
            return message

    async def pending_chats(self) -> list[str]:
        async with self.sessions() as s:
            return list((await s.scalars(select(ChatMessage.chat_id).where(
                ChatMessage.routing_status == 'pending').distinct())).all())

    async def claim_message(self, chat_id: str) -> ChatMessage | None:
        async with self.transaction() as s:
            m = await s.scalar(select(ChatMessage).where(ChatMessage.chat_id == chat_id,
                                ChatMessage.routing_status == 'pending').order_by(ChatMessage.sequence).limit(1))
            if m:
                m.routing_status = 'routing'
            return m

    async def routing_context(self, message: ChatMessage) -> dict:
        async with self.sessions() as s:
            rows = (await s.execute(select(TaskRecord, TaskVersion).join(TaskVersion,
                (TaskVersion.task_id == TaskRecord.task_id) & (TaskVersion.version == TaskRecord.version))
                .where(TaskRecord.chat_id == message.chat_id).order_by(TaskVersion.created_at.desc())
                .limit(self.settings.context_tasks))).all()
            history = list((await s.scalars(select(ChatMessage).where(ChatMessage.chat_id == message.chat_id,
                ChatMessage.sequence < message.sequence).order_by(ChatMessage.sequence.desc())
                .limit(self.settings.context_messages))).all())
            return {'chat_id': message.chat_id, 'sender': message.user_id, 'new_message': message.content,
                    'tasks': [{'task_id': t.task_id, 'user_id': t.user_id, 'version': v.version,
                               'content': v.content[:4000], 'status': v.status} for t, v in reversed(rows)],
                    'history': [{'user_id': m.user_id, 'content': m.content[:2000], 'task_id': m.task_id}
                                for m in reversed(history)]}

    async def apply_route(self, message_id: str, decision: RouteDecision) -> Key | None:
        async with self.transaction() as s:
            message = await s.get(ChatMessage, message_id)
            if message is None:
                raise DomainError('Message not found', 404)
            if message.routing_status not in {'pending', 'routing'}:
                return None
            refs: list[Key] = []
            for task_id in decision.target_task_ids:
                target = await s.get(TaskRecord, task_id)
                if target is None or target.chat_id != message.chat_id:
                    raise DomainError('Task reference is not available in this chat', 422)
                if decision.action == 'follow_up' and target.user_id != message.user_id:
                    raise DomainError('Only the owner can revise a task', 403)
                refs.append((target.task_id, target.version))
            if decision.action == 'clarification':
                message.routing_status = 'clarification'
                await self._event(s, message.chat_id, 'clarification_request',
                                  {'question': decision.reason}, message_id=message_id, dedupe=f'route:{message_id}')
                return None
            if decision.action == 'follow_up':
                task = await s.get(TaskRecord, refs[0][0])
                old = await s.get(TaskVersion, refs[0])
                v = await self._revise(s, task, old.content + '\nFollow-up instruction: ' + message.content, message_id)
            else:
                task = TaskRecord(task_id=str(uuid4()), message_id=message_id, chat_id=message.chat_id,
                                  user_id=message.user_id, version=1, content=message.content, status='queued')
                s.add(task)
                await s.flush()
                v = TaskVersion(task_id=task.task_id, version=1, message_id=message_id, content=message.content, status='queued')
                s.add(v)
                await s.flush()
                await self._add_edges(s, task, v, refs)
                await self._event(s, task.chat_id, 'task_status', {'status': 'queued', 'current': True}, v)
            message.task_id, message.routing_status = task.task_id, 'routed'
            await self._settle(s)
            return v.task_id, v.version

    async def route_failed(self, message_id: str, error: str) -> None:
        async with self.transaction() as s:
            m = await s.get(ChatMessage, message_id)
            if m.routing_status in {'pending', 'routing'}:
                m.routing_status = 'failed'
                await self._event(s, m.chat_id, 'failure_notification', {'error': error, 'stage': 'routing'},
                                  message_id=m.message_id, dedupe=f'route:{m.message_id}')

    async def _revise(self, s: AsyncSession, task: TaskRecord, content: str,
                      message_id: str | None, replacements: dict[str, int] | None = None) -> TaskVersion:
        old_key = (task.task_id, task.version)
        edges = await self._edges(s, old_key)
        replacements = replacements or {}
        refs = [(e.prerequisite_task_id, replacements.get(e.prerequisite_task_id, e.prerequisite_version)) for e in edges]
        await self._validate_edges(s, task, refs)
        task.version += 1
        v = TaskVersion(task_id=task.task_id, version=task.version, message_id=message_id,
                        content=content, status='queued')
        task.content, task.status, task.result = content, 'queued', None
        s.add(v)
        await s.flush()
        await self._add_edges(s, task, v, refs)
        # Pending questions follow the current dependent version. Old snapshots keep their selected inputs.
        for d in await self._pending(s, old_key):
            d.version = v.version
            await self._decision_event(s, d)
        await self._event(s, task.chat_id, 'task_status', {'status': 'queued', 'current': True}, v)
        await self._notify_revision(s, task, v)
        return v

    async def _decision_event(self, s: AsyncSession, d: DependencyDecision) -> None:
        owner = await s.get(TaskRecord, d.task_id)
        parent = await s.get(TaskRecord, d.prerequisite_task_id)
        v = await s.get(TaskVersion, (d.task_id, d.version))
        await self._event(s, owner.chat_id, 'dependency_decision_request',
            {**DecisionResponse.model_validate(d).model_dump(), 'user_id': owner.user_id,
             'question': f'{parent.user_id} updated their task. Would you like to use the original version or the updated version?'}, v)

    async def _notify_revision(self, s: AsyncSession, task: TaskRecord, new: TaskVersion) -> None:
        affected = (await s.execute(select(TaskDependency, TaskRecord, TaskVersion)
            .join(TaskRecord, TaskRecord.task_id == TaskDependency.task_id)
            .join(TaskVersion, (TaskVersion.task_id == TaskDependency.task_id) & (TaskVersion.version == TaskDependency.version))
            .where(TaskDependency.prerequisite_task_id == task.task_id,
                   TaskDependency.version == TaskRecord.version,
                   TaskVersion.status.not_in(['failed', 'cancelled'])))).all()
        for edge, dependent, v in affected:
            existing = next((d for d in await self._pending(s, (v.task_id, v.version))
                             if d.prerequisite_task_id == task.task_id), None)
            if existing:
                existing.updated_version = new.version
                d = existing
            else:
                d = DependencyDecision(decision_id=str(uuid4()), task_id=dependent.task_id, version=v.version,
                    prerequisite_task_id=task.task_id, original_version=edge.prerequisite_version,
                    updated_version=new.version, status='pending')
                s.add(d)
            await s.flush()
            await self._status(s, v, 'waiting_for_decision')
            await self._decision_event(s, d)

    async def decide(self, decision_id: str, user_id: str, choice: str) -> DecisionResponse:
        async with self.transaction() as s:
            d = await s.get(DependencyDecision, decision_id)
            if d is None:
                raise DomainError('Decision not found', 404)
            task = await s.get(TaskRecord, d.task_id)
            if task.user_id != user_id:
                raise DomainError('Only the dependent task owner can choose', 403)
            if choice not in {'keep_original', 'use_updated'}:
                raise DomainError('Invalid choice', 422)
            if d.status != 'pending':
                if d.status == 'resolved' and d.choice == choice:
                    return DecisionResponse.model_validate(d)
                raise DomainError('Decision is no longer pending')
            if task.version != d.version:
                raise DomainError('Decision does not match the current task version')
            d.status, d.choice, d.resolved_at = 'resolved', choice, utcnow()
            if choice == 'use_updated':
                old = await s.get(TaskVersion, (task.task_id, task.version))
                await self._revise(s, task, old.content, old.message_id,
                                   {d.prerequisite_task_id: d.updated_version})
            await self._settle(s)
            await self._event(s, task.chat_id, 'task_status',
                              {'decision_id': d.decision_id, 'choice': choice, 'current_version': task.version},
                              await s.get(TaskVersion, (task.task_id, task.version)))
            return DecisionResponse.model_validate(d)

    async def cancel(self, task_id: str) -> TaskResponse:
        async with self.transaction() as s:
            t = await s.get(TaskRecord, task_id)
            if t is None:
                raise DomainError('Task not found', 404)
            v = await s.get(TaskVersion, (task_id, t.version))
            if v.status != 'cancelled':
                if v.status not in ACTIVE:
                    raise DomainError('Task has already finished')
                for d in await self._pending(s, (task_id, t.version)):
                    d.status, d.resolved_at = 'dismissed', utcnow()
                await self._status(s, v, 'cancelled', 'Explicitly cancelled by user')
                await self._settle(s)
        return await self.task(task_id)

    async def recover(self) -> None:
        async with self.transaction() as s:
            for m in (await s.scalars(select(ChatMessage).where(ChatMessage.routing_status == 'routing'))).all():
                m.routing_status = 'pending'
            for v in (await s.scalars(select(TaskVersion).where(TaskVersion.status == 'running'))).all():
                await self._status(s, v, 'queued')
            await self._settle(s)

    async def work_state(self) -> dict[Key, tuple[str, str | None]]:
        async with self.transaction() as s:
            await self._settle(s)
            return {(v.task_id, v.version): (v.status, v.run_token) for v in
                    (await s.scalars(select(TaskVersion).where(TaskVersion.status.in_(ACTIVE)))).all()}

    async def claim(self, key: Key) -> dict | None:
        async with self.transaction() as s:
            await self._settle(s)
            v = await s.get(TaskVersion, key)
            if v is None or v.status != 'queued':
                return None
            token = str(uuid4())
            v.attempts += 1
            await self._status(s, v, 'running')
            v.run_token = token
            t = await s.get(TaskRecord, key[0])
            parents = []
            for edge in await self._edges(s, key):
                p = await s.get(TaskVersion, (edge.prerequisite_task_id, edge.prerequisite_version))
                owner = await s.get(TaskRecord, p.task_id)
                parents.append({'task_id': p.task_id, 'version': p.version, 'user_id': owner.user_id,
                                'instructions': p.content, 'result': p.result})
            origin = await s.get(ChatMessage, v.message_id) if v.message_id else None
            query = select(ChatMessage).where(ChatMessage.chat_id == t.chat_id)
            if origin:
                query = query.where(ChatMessage.sequence <= origin.sequence)
            history = list((await s.scalars(query.order_by(ChatMessage.sequence.desc())
                                           .limit(self.settings.context_messages))).all())
            return {'run_token': token, 'chat_id': t.chat_id, 'instructions': v.content, 'prerequisites': parents,
                    'conversation': [{'user_id': m.user_id, 'content': m.content[:2000]} for m in reversed(history)]}

    async def finish(self, key: Key, token: str, result: str | None = None, error: str | None = None) -> bool:
        async with self.transaction() as s:
            v = await s.get(TaskVersion, key)
            if v is None or v.status != 'running' or v.run_token != token:
                return False
            if result is None and error is None:
                raise ValueError('Completion requires a result or error')
            v.result = result if error is None else None
            await self._status(s, v, 'failed' if error else 'completed', error)
            t = await s.get(TaskRecord, key[0])
            if error is None and t.version == key[1] and not await self._pending(s, key):
                await self._event(s, t.chat_id, 'agent_answer', {'content': result}, v,
                                  dedupe=f'answer:{key[0]}:{key[1]}')
            await self._settle(s)
            return True

    async def release(self, key: Key, token: str) -> None:
        """Shutdown/unexpected cancellation is retryable, unlike explicit cancellation."""
        async with self.transaction() as s:
            v = await s.get(TaskVersion, key)
            if v and v.status == 'running' and v.run_token == token:
                await self._status(s, v, 'queued')
                await self._settle(s)

    async def task(self, task_id: str, version: int | None = None) -> TaskResponse:
        async with self.sessions() as s:
            t = await s.get(TaskRecord, task_id)
            if t is None:
                raise DomainError('Task not found', 404)
            v = await s.get(TaskVersion, (task_id, version or t.version))
            if v is None:
                raise DomainError('Version not found', 404)
            return TaskResponse(**VersionResponse.model_validate(v).model_dump(), chat_id=t.chat_id,
                user_id=t.user_id, original_message_id=t.message_id,
                dependencies=[DependencyResponse.model_validate(e) for e in await self._edges(s, (task_id, v.version))],
                pending_decisions=[DecisionResponse.model_validate(d) for d in await self._pending(s, (task_id, v.version))])

    async def events(self, chat_id: str, after: int, limit: int) -> EventPage:
        async with self.sessions() as s:
            events = list((await s.scalars(select(ChatEvent).where(ChatEvent.chat_id == chat_id,
                ChatEvent.sequence > after).order_by(ChatEvent.sequence).limit(limit))).all())
            return EventPage(events=[EventResponse.model_validate(e) for e in events],
                             next_cursor=events[-1].sequence if events else after)

    async def cleanup(self, dry_run: bool = True) -> list[Key]:
        """Delete expired terminal leaves only; references protect their whole ancestry."""
        cutoff = utcnow() - timedelta(days=self.settings.retention_days)
        async with self.transaction() as s:
            tasks = {t.task_id: t.version for t in (await s.scalars(select(TaskRecord))).all()}
            edges = list((await s.scalars(select(TaskDependency))).all())
            decisions = list((await s.scalars(select(DependencyDecision))).all())
            protected = {(e.prerequisite_task_id, e.prerequisite_version) for e in edges}
            for d in decisions:
                if d.status == 'pending':
                    protected.update({(d.task_id, d.version), (d.prerequisite_task_id, d.original_version),
                                      (d.prerequisite_task_id, d.updated_version)})
            candidates = list((await s.scalars(select(TaskVersion).where(
                TaskVersion.status.in_(['completed', 'failed', 'cancelled']), TaskVersion.completed_at < cutoff))).all())
            keys = [(v.task_id, v.version) for v in candidates
                    if tasks[v.task_id] != v.version and (v.task_id, v.version) not in protected]
            if not dry_run:
                for key in keys:
                    # Resolved decision audit survives in chat_events; remove its FK record if expiring.
                    for d in decisions:
                        if d.status != 'pending' and (key == (d.task_id, d.version) or
                            key in {(d.prerequisite_task_id, d.original_version), (d.prerequisite_task_id, d.updated_version)}):
                            await s.delete(d)
                    await s.execute(delete(TaskDependency).where(TaskDependency.task_id == key[0], TaskDependency.version == key[1]))
                    await s.flush()
                    await s.delete(await s.get(TaskVersion, key))
            return keys
