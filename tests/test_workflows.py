import asyncio
from collections import defaultdict
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import complete, new_task, revise
from crud import DomainError, Store, is_bot_mentioned
from models import ChatEvent, TaskVersion, utcnow
from schemas import MessageRequest, RouteDecision


async def eventually(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.01)


class GateExecutor:
    def __init__(self):
        self.started = asyncio.Queue()
        self.gates = defaultdict(asyncio.Event)
        self.inputs = []
        self.active = 0
        self.maximum = 0
        self.cancelled = 0

    async def execute(self, inputs):
        self.inputs.append(inputs)
        name = inputs['instructions']
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await self.started.put(name)
        try:
            await self.gates[name].wait()
            return 'answer: ' + name
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


async def task_status(store, key, status):
    return (await store.task(*key)).status == status


async def test_ordinary_message_no_routing(store, schedulers):
    from conftest import NewRouter
    router, executor = NewRouter(), GateExecutor()
    await schedulers(executor, router)
    m = await store.submit(MessageRequest(chat_id='chat', user_id='alice', content='hello team'))
    await asyncio.sleep(0.04)
    assert m.routing_status == 'stored_no_action'
    assert router.calls == 0 and not executor.inputs
    assert not is_bot_mentioned('abc@CastleBot')
    assert is_bot_mentioned('Hi @castlebot!')


async def test_creation_and_idempotent_routing(store):
    m = await store.submit(MessageRequest(chat_id='chat', user_id='alice', content='@CastleBot explain this'))
    decision = RouteDecision(action='new_task', target_task_ids=[], reason='New')
    key = await store.apply_route(m.message_id, decision)
    assert await store.apply_route(m.message_id, decision) is None
    task = await store.task(key[0])
    assert task.version == 1 and task.status == 'queued'
    assert task.message_id == m.message_id


async def test_concurrency_limit_and_no_global_fifo(store, schedulers):
    keys = [await new_task(store, str(i)) for i in range(5)]
    executor = GateExecutor()
    scheduler = await schedulers(executor, max_concurrent_agents=3)
    started = [await asyncio.wait_for(executor.started.get(), 3) for _ in range(3)]
    assert executor.active == 3 and executor.maximum == 3
    assert not scheduler.schedule_worker(keys[0])
    fast = started[1]
    executor.gates[fast].set()
    await asyncio.wait_for(executor.started.get(), 3)
    assert executor.maximum == 3
    events = await store.events('chat', 0, 200)
    assert [e.payload['content'] for e in events.events if e.type == 'agent_answer'] == ['answer: ' + fast]
    for key in keys:
        executor.gates[(await store.task(*key)).content].set()
    async def done():
        return all([(await store.task(*key)).status == 'completed' for key in keys]) and not scheduler.workers
    await eventually(done)


async def test_revision_retains_pinned_old_worker_and_suppresses_answer(store):
    a = await new_task(store, 'original')
    c = await new_task(store, 'compare', user='jack', refs=[a[0]])
    inputs = await store.claim(a)
    a2 = await revise(store, a[0])
    assert a2 == (a[0], 2)
    old = await store.task(*a)
    assert old.content == '@CastleBot original' and old.status == 'running'
    assert await store.finish(a, inputs['run_token'], result='old findings')
    await complete(store, a2, 'new findings')
    assert (await store.task(*a)).result == 'old findings'
    assert (await store.task(a[0])).result == 'new findings'
    answers = [e for e in (await store.events('chat', 0, 200)).events if e.type == 'agent_answer']
    assert [(e.task_id, e.version) for e in answers] == [a2]
    assert (await store.task(*c)).status == 'waiting_for_decision'


async def test_unreferenced_old_worker_cannot_complete(store):
    a = await new_task(store)
    inputs = await store.claim(a)
    a2 = await revise(store, a[0])
    assert (await store.task(*a)).status == 'cancelled'
    assert not await store.finish(a, inputs['run_token'], result='STALE')
    await complete(store, a2, 'current')
    assert (await store.task(a[0])).result == 'current'


async def test_cancel_before_start_and_while_running(store, schedulers):
    a, b = await new_task(store, 'first'), await new_task(store, 'second')
    await store.cancel(b[0])
    executor = GateExecutor()
    scheduler = await schedulers(executor, max_concurrent_agents=1)
    await asyncio.wait_for(executor.started.get(), 3)
    await store.cancel(a[0])
    scheduler.notify()
    async def cancelled():
        return executor.cancelled == 1 and not scheduler.workers
    await eventually(cancelled)
    assert len(executor.inputs) == 1
    assert (await store.task(*a)).status == (await store.task(*b)).status == 'cancelled'
    assert (await store.cancel(a[0])).status == 'cancelled'


async def test_dependencies_wait_and_use_saved_results(store, schedulers):
    a, b = await new_task(store, 'A'), await new_task(store, 'B', user='bob')
    c = await new_task(store, 'compare', user='jack', refs=[a[0], b[0]])
    assert (await store.task(*c)).status == 'waiting_for_dependencies'
    executor = GateExecutor()
    scheduler = await schedulers(executor, max_concurrent_agents=2)
    first = [await asyncio.wait_for(executor.started.get(), 3) for _ in range(2)]
    assert c not in scheduler.workers
    executor.gates[first[0]].set()
    await eventually(lambda: task_status(store, a if first[0].endswith('A') else b, 'completed'))
    assert len(executor.inputs) == 2
    executor.gates[first[1]].set()
    assert await asyncio.wait_for(executor.started.get(), 3) == '@CastleBot compare'
    parents = executor.inputs[-1]['prerequisites']
    assert {p['result'] for p in parents} == {'answer: @CastleBot A', 'answer: @CastleBot B'}
    executor.gates['@CastleBot compare'].set()
    await eventually(lambda: task_status(store, c, 'completed'))
    assert len(executor.inputs) == 3


@pytest.mark.parametrize('failure', ['failed', 'cancelled'])
async def test_upstream_failure_propagates(store, failure):
    a = await new_task(store)
    b = await new_task(store, refs=[a[0]], user='bob')
    c = await new_task(store, refs=[b[0]], user='jack')
    if failure == 'failed':
        inputs = await store.claim(a)
        await store.finish(a, inputs['run_token'], error='API unavailable')
    else:
        await store.cancel(a[0])
    assert (await store.task(*b)).status == (await store.task(*c)).status == 'failed'
    assert await store.claim(c) is None


@pytest.mark.parametrize('choice', ['keep_original', 'use_updated'])
async def test_dependency_decisions(store, choice):
    a = await new_task(store, 'A')
    b = await new_task(store, 'B', user='bob')
    c = await new_task(store, 'compare', user='jack', refs=[a[0], b[0]])
    old_inputs = await store.claim(a)
    a2 = await revise(store, a[0], 'A2')
    d = (await store.task(*c)).pending_decisions[0]
    assert d.answer_published is False
    assert (await store.task(*c)).status == 'waiting_for_decision'
    with pytest.raises(DomainError, match='owner'):
        await store.decide(d.decision_id, 'alice', choice)
    await store.decide(d.decision_id, 'jack', choice)
    # Repeated submissions are idempotent.
    await store.decide(d.decision_id, 'jack', choice)
    if choice == 'keep_original':
        assert await store.finish(a, old_inputs['run_token'], result='original A')
    else:
        assert not await store.finish(a, old_inputs['run_token'], result='STALE A')
    await complete(store, a2, 'updated A')
    await complete(store, b, 'B findings')
    current = await store.task(c[0])
    inputs = await complete(store, (current.task_id, current.version), 'comparison')
    assert {p['result'] for p in inputs['prerequisites']} == {'B findings', 'original A' if choice == 'keep_original' else 'updated A'}
    assert current.version == (1 if choice == 'keep_original' else 2)


async def test_running_dependent_paused_and_run_token_rejects_late_completion(store):
    a = await new_task(store)
    await complete(store, a, 'old A')
    c = await new_task(store, user='jack', refs=[a[0]])
    old = await store.claim(c)
    await revise(store, a[0])
    d = (await store.task(*c)).pending_decisions[0]
    assert d.answer_published is False
    assert (await store.task(*c)).status == 'waiting_for_decision'
    assert not await store.finish(c, old['run_token'], result='stale C')
    await store.decide(d.decision_id, 'jack', 'keep_original')
    current = await store.claim(c)
    assert current['run_token'] != old['run_token']
    assert not await store.finish(c, old['run_token'], result='stale C')
    assert await store.finish(c, current['run_token'], result='fresh C')


async def test_completed_dependent_updated_preserves_published_history(store):
    a = await new_task(store)
    await complete(store, a)
    c = await new_task(store, user='jack', refs=[a[0]])
    await complete(store, c, 'old comparison')
    completed_at = (await store.task(*c)).completed_at
    a2 = await revise(store, a[0])
    pending = await store.task(*c)
    assert pending.status == 'completed' and pending.result == 'old comparison'
    assert pending.completed_at == completed_at
    d = pending.pending_decisions[0]
    assert d.answer_published is True
    question = next(e for e in (await store.events('chat', 0, 200)).events
                    if e.type == 'dependency_decision_request')
    assert question.payload['answer_published'] is True
    assert 'complete and remains in the chat' in question.payload['question']
    await store.decide(d.decision_id, 'jack', 'use_updated')
    assert (await store.task(*c)).status == 'completed'
    assert (await store.task(*c)).completed_at == completed_at
    assert (await store.task(c[0])).version == 2
    assert (await store.task(c[0])).status == 'waiting_for_dependencies'
    await complete(store, a2, 'updated')
    await complete(store, (c[0], 2), 'updated comparison')
    answers = [e.payload['content'] for e in (await store.events('chat', 0, 200)).events if e.type == 'agent_answer']
    assert 'old comparison' in answers and 'updated comparison' in answers


async def test_completed_dependent_keep_original_is_noop_for_published_result(store):
    a = await new_task(store)
    await complete(store, a, 'first findings')
    c = await new_task(store, user='jack', refs=[a[0]])
    await complete(store, c, 'published pitch')
    original = await store.task(*c)
    await revise(store, a[0])
    decision = (await store.task(*c)).pending_decisions[0]
    assert decision.answer_published is True
    resolved = await store.decide(decision.decision_id, 'jack', 'keep_original')
    assert resolved.answer_published is True
    current = await store.task(c[0])
    assert current.version == 1 and current.status == 'completed'
    assert current.result == original.result and current.completed_at == original.completed_at
    answers = [e for e in (await store.events('chat', 0, 200)).events
               if e.type == 'agent_answer' and e.task_id == c[0]]
    assert len(answers) == 1 and answers[0].payload['content'] == 'published pitch'


async def test_recovery_repairs_legacy_published_decision_status(store):
    a = await new_task(store)
    await complete(store, a)
    c = await new_task(store, user='jack', refs=[a[0]])
    await complete(store, c, 'already published')
    await revise(store, a[0])
    original = await store.task(*c)
    async with store.transaction() as s:
        version = await s.get(TaskVersion, c)
        await store._status(s, version, 'waiting_for_decision')
    assert (await store.task(*c)).status == 'waiting_for_decision'
    await store.recover()
    repaired = await store.task(*c)
    assert repaired.status == 'completed' and repaired.result == 'already published'
    assert repaired.completed_at == original.completed_at
    assert repaired.pending_decisions[0].answer_published is True


async def test_two_concurrent_revisions_and_pending_offer_coalescing(store):
    a = await new_task(store)
    c = await new_task(store, user='jack', refs=[a[0]])
    revisions = await asyncio.gather(revise(store, a[0], 'first'), revise(store, a[0], 'second'))
    assert {v for _, v in revisions} == {2, 3}
    task = await store.task(a[0])
    assert task.version == 3 and 'first' in task.content and 'second' in task.content
    decisions = (await store.task(*c)).pending_decisions
    assert len(decisions) == 1 and decisions[0].original_version == 1 and decisions[0].updated_version == 3


async def test_two_changed_prerequisites_preserve_pending_decisions(store):
    a, b = await new_task(store), await new_task(store, user='bob')
    c = await new_task(store, user='jack', refs=[a[0], b[0]])
    a2 = await revise(store, a[0])
    b2 = await revise(store, b[0], user='bob')
    ds = (await store.task(*c)).pending_decisions
    assert len(ds) == 2
    await store.decide(ds[0].decision_id, 'jack', 'use_updated')
    current = await store.task(c[0])
    assert current.version == 2 and len(current.pending_decisions) == 1
    await store.decide(ds[1].decision_id, 'jack', 'use_updated')
    assert (await store.task(c[0])).version == 3
    await complete(store, a2)
    await complete(store, b2)
    await complete(store, (c[0], 3))


async def test_invalid_cross_chat_and_owner_references(store):
    a = await new_task(store, chat='other')
    with pytest.raises(DomainError, match='chat'):
        await new_task(store, refs=[a[0]])
    with pytest.raises(DomainError, match='chat'):
        await new_task(store, refs=['missing'])
    own = await new_task(store)
    with pytest.raises(DomainError, match='owner'):
        await revise(store, own[0], user='bob')
    assert (await store.task(own[0])).version == 1


async def test_cycle_rejected_atomically(store):
    a = await new_task(store)
    b = await new_task(store, user='bob', refs=[a[0]])
    from models import TaskRecord
    async with store.transaction() as s:
        task = await s.get(TaskRecord, a[0])
        with pytest.raises(DomainError, match='cycle'):
            await store._validate_edges(s, task, [b])
        with pytest.raises(DomainError, match='cycle'):
            await store._validate_edges(s, task, [a])


async def test_restart_recovers_running_routing_and_waiting(store, db):
    a = await new_task(store)
    old = await store.claim(a)
    c = await new_task(store, user='jack', refs=[a[0]])
    m = await store.submit(MessageRequest(chat_id='another', user_id='alice', content='@CastleBot waiting route'))
    await store.claim_message('another')
    restarted = Store(db[1], db[2])
    await restarted.recover()
    assert (await restarted.task(*a)).status == 'queued'
    assert (await restarted.task(*c)).status == 'waiting_for_dependencies'
    assert (await restarted.claim_message('another')).message_id == m.message_id
    assert not await restarted.finish(a, old['run_token'], result='lost attempt')
    await complete(restarted, a)
    await complete(restarted, c)


async def test_publication_order_cursor_idempotency_and_atomicity(store):
    keys = [await new_task(store, str(i)) for i in range(4)]
    claimed = [await store.claim(k) for k in keys]
    await asyncio.gather(*[store.finish(k, i['run_token'], result=str(n)) for n, (k, i) in enumerate(zip(keys, claimed))])
    for k, inputs in zip(keys, claimed):
        assert not await store.finish(k, inputs['run_token'], result='duplicate')
    events = (await store.events('chat', 0, 200)).events
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert len([e for e in events if e.type == 'agent_answer']) == 4
    page = await store.events('chat', 0, 3)
    again = await store.events('chat', 0, 3)
    assert page == again
    assert (await store.events('chat', page.next_cursor, 200)).events == events[3:]
    async with store.sessions() as s:
        for answer in (await s.scalars(select(ChatEvent).where(ChatEvent.type == 'agent_answer'))).all():
            version = await s.get(TaskVersion, (answer.task_id, answer.version))
            assert version.status == 'completed' and version.result == answer.payload['content']


async def test_retention_dry_run_and_protection(store):
    a = await new_task(store)
    await complete(store, a)
    b = await new_task(store, user='bob')
    failed = await store.claim(b)
    assert await store.finish(b, failed['run_token'], error='not available')
    c = await new_task(store, user='jack', refs=[a[0]])
    await revise(store, a[0])
    await revise(store, b[0], user='bob')
    async with store.transaction() as s:
        for key in [a, b]:
            (await s.get(TaskVersion, key)).completed_at = utcnow() - timedelta(days=40)
    assert await store.cleanup() == [b]
    assert (await store.task(*b)).status == 'failed'
    assert await store.cleanup(dry_run=False) == [b]
    with pytest.raises(DomainError, match='Version'):
        await store.task(*b)
    assert (await store.task(*a)).status == 'completed'
    assert (await store.task(*c)).pending_decisions
    assert (await store.task(b[0])).version == 2


async def test_retention_keeps_published_historical_answer(store):
    a = await new_task(store)
    await complete(store, a)
    c = await new_task(store, user='jack', refs=[a[0]])
    await complete(store, c, 'published first answer')
    a2 = await revise(store, a[0])
    decision = (await store.task(*c)).pending_decisions[0]
    await store.decide(decision.decision_id, 'jack', 'use_updated')
    await complete(store, a2)
    await complete(store, (c[0], 2), 'published second answer')
    async with store.transaction() as s:
        (await s.get(TaskVersion, c)).completed_at = utcnow() - timedelta(days=40)
    assert c not in await store.cleanup()
    await store.cleanup(dry_run=False)
    old = await store.task(*c)
    assert old.status == 'completed' and old.result == 'published first answer'


async def test_ordinary_execution_failure_and_timeout(store, schedulers):
    class Fails:
        async def execute(self, _):
            raise RuntimeError('do not persist secret-token')
    a = await new_task(store)
    await schedulers(Fails())
    await eventually(lambda: task_status(store, a, 'failed'))
    assert (await store.task(*a)).error == 'Execution failed (RuntimeError)'
    assert 'secret-token' not in str(await store.events('chat', 0, 200))


async def test_timeout_releases_slot(store, schedulers):
    a = await new_task(store)
    executor = GateExecutor()
    scheduler = await schedulers(executor, task_timeout_seconds=0.03)
    await eventually(lambda: task_status(store, a, 'failed'))
    assert 'timeout' in (await store.task(*a)).error
    async def empty():
        return not scheduler.workers
    await eventually(empty)


async def test_transitive_old_version_retention(store):
    a = await new_task(store)
    c = await new_task(store, user='jack', refs=[a[0]])
    d = await new_task(store, user='dana', refs=[c[0]])
    old = await store.claim(a)
    a2 = await revise(store, a[0])
    c_decision = (await store.task(*c)).pending_decisions[0]
    await store.decide(c_decision.decision_id, 'jack', 'use_updated')
    d_decision = (await store.task(*d)).pending_decisions[0]
    await store.decide(d_decision.decision_id, 'dana', 'keep_original')
    assert await store.finish(a, old['run_token'], result='old A')
    await complete(store, c, 'old C')
    inputs = await complete(store, d)
    assert inputs['prerequisites'][0]['result'] == 'old C'
    await complete(store, a2)
    await complete(store, (c[0], 2))


async def test_cancel_worker_queued_behind_semaphore(store, schedulers):
    a = await new_task(store, 'first')
    b = await new_task(store, 'second')
    executor = GateExecutor()
    scheduler = await schedulers(executor, max_concurrent_agents=1)
    started = await asyncio.wait_for(executor.started.get(), 3)
    queued = b if started.endswith('first') else a
    assert queued in scheduler.workers
    await store.cancel(queued[0])
    scheduler.notify()
    executor.gates[started].set()
    async def empty():
        return not scheduler.workers
    await eventually(empty)
    assert len(executor.inputs) == 1
    assert (await store.task(*queued)).status == 'cancelled'


async def test_scheduler_reclaims_orphaned_running_version(store, schedulers):
    a = await new_task(store)
    executor = GateExecutor()
    scheduler = await schedulers(executor)
    await asyncio.wait_for(executor.started.get(), 3)
    worker = scheduler.workers[a]
    old_token = worker.token
    # Simulate a transient DB error during completion; the job exits without committing it.
    original_finish = store.finish
    calls = 0
    async def flaky_finish(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('temporary database outage')
        return await original_finish(*args, **kwargs)
    store.finish = flaky_finish
    executor.gates['@CastleBot task'].set()
    await eventually(lambda: task_status(store, a, 'completed'))
    assert calls == 2
    assert not await store.finish(a, old_token, result='duplicate stale attempt')


async def test_repeated_decision_conflict_and_cancel_dismisses_questions(store):
    a = await new_task(store)
    c = await new_task(store, user='jack', refs=[a[0]])
    await revise(store, a[0])
    decision = (await store.task(*c)).pending_decisions[0]
    await store.decide(decision.decision_id, 'jack', 'keep_original')
    with pytest.raises(DomainError, match='no longer pending'):
        await store.decide(decision.decision_id, 'jack', 'use_updated')
    await revise(store, a[0], 'third')
    decision = (await store.task(*c)).pending_decisions[0]
    await store.cancel(c[0])
    assert not (await store.task(*c)).pending_decisions
    with pytest.raises(DomainError, match='no longer pending'):
        await store.decide(decision.decision_id, 'jack', 'use_updated')
