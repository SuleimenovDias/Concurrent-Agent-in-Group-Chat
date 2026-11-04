import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import BadRequestError, RateLimitError

from conftest import NewRouter
from executor import OpenAIExecutor, OpenAIGateway, with_retries
from main import create_app
from router import OpenAIRouter, parse_route_decision
from schemas import RouteDecision
from test_workflows import GateExecutor, eventually


async def test_rest_api_and_quick_ack(db):
    engine, sessions, settings = db
    executor = GateExecutor()
    app = create_app(settings, sessions, engine, router=NewRouter(), executor=executor)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json() == {'status': 'ok'}
            assert (await client.get('/db-health')).status_code == 200
            assert (await client.get('/tasks/missing')).status_code == 404
            assert (await client.post('/messages', json={'content': ''})).status_code == 422
            ordinary = await client.post('/messages', json={'chat_id': 'room', 'user_id': 'bob', 'content': 'Hello'})
            assert ordinary.status_code == 202 and ordinary.json()['status'] == 'stored_no_action'
            submitted = await client.post('/messages', json={'chat_id': 'room', 'user_id': 'alice', 'content': '@CastleBot explain'})
            assert submitted.status_code == 202 and submitted.json()['task_id'] is None
            await asyncio.wait_for(executor.started.get(), 3)
            message = (await client.get('/messages/' + submitted.json()['message_id'])).json()
            assert message['routing_status'] == 'routed'
            task_id = message['task_id']
            task = (await client.get('/tasks/' + task_id)).json()
            assert task['status'] == 'running'
            assert len((await client.get('/chats/room/tasks')).json()) == 1
            history = (await client.get('/chats/room/messages')).json()
            assert len(history) == 2 and history[1]['sequence'] == 2
            assert len((await client.get('/chats/room/messages?after=1')).json()) == 1
            assert (await client.get(f'/tasks/{task_id}/versions/1')).status_code == 200
            assert len((await client.get(f'/tasks/{task_id}/versions')).json()) == 1
            assert (await client.get('/chats/room/decisions')).json() == []
            assert (await client.get('/chats/other/events')).json()['events'] == []
            executor.gates['@CastleBot explain'].set()
            async def completed():
                return (await client.get('/tasks/' + task_id)).json()['status'] == 'completed'
            await eventually(completed)
            events = (await client.get('/chats/room/events')).json()
            assert any(e['type'] == 'agent_answer' for e in events['events'])
            assert (await client.get('/chats/room/events?after=' + str(events['next_cursor']))).json()['events'] == []
            assert (await client.post('/tasks/' + task_id + '/cancel')).status_code == 409


class DemoRouter:
    async def route(self, context):
        text = context['new_message']
        tasks = {t['user_id']: t['task_id'] for t in context['tasks']}
        if 'compare' in text:
            return RouteDecision(action='dependent_task', target_task_ids=[tasks['alice'], tasks['bob']], reason='Compare findings')
        if 'actually' in text:
            return RouteDecision(action='follow_up', target_task_ids=[tasks['alice']], reason='Refine focus')
        return RouteDecision(action='new_task', target_task_ids=[], reason='New')


@pytest.mark.parametrize('choice', ['keep_original', 'use_updated'])
async def test_alice_bob_jack_end_to_end(db, choice):
    engine, sessions, settings = db
    executor = GateExecutor()
    app = create_app(settings, sessions, engine, router=DemoRouter(), executor=executor)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def send(user, content):
                r = await client.post('/messages', json={'chat_id': 'demo', 'user_id': user, 'content': '@CastleBot ' + content})
                assert r.status_code == 202
                return r.json()['message_id']
            await send('alice', "explain NVIDIA's business model")
            await send('bob', "explain AMD's business model")
            originals = [await asyncio.wait_for(executor.started.get(), 3) for _ in range(2)]
            assert executor.active == 2
            jack_message = await send('jack', "compare Alice's and Bob's findings")
            async def jack_waiting():
                m = (await client.get('/messages/' + jack_message)).json()
                return m['routing_status'] == 'routed'
            await eventually(jack_waiting)
            jack = (await client.get('/messages/' + jack_message)).json()['task_id']
            assert (await client.get('/tasks/' + jack)).json()['status'] == 'waiting_for_dependencies'
            await send('alice', "actually focus on NVIDIA's data-center business")
            async def has_decision():
                return bool((await client.get('/chats/demo/decisions')).json())
            await eventually(has_decision)
            d = (await client.get('/chats/demo/decisions')).json()[0]
            assert (await client.post('/dependency-decisions/' + d['decision_id'] + '/resolve',
                                     json={'user_id': 'jack', 'choice': choice})).status_code == 200
            updated = await asyncio.wait_for(executor.started.get(), 3)
            for name in originals + [updated]:
                executor.gates[name].set()
            comparison = await asyncio.wait_for(executor.started.get(), 3)
            assert 'compare' in comparison
            parents = executor.inputs[-1]['prerequisites']
            alice_parent = next(p for p in parents if p['user_id'] == 'alice')
            assert alice_parent['version'] == (1 if choice == 'keep_original' else 2)
            assert alice_parent['result'].startswith('answer:')
            executor.gates[comparison].set()
            async def done():
                return (await client.get('/tasks/' + jack)).json()['status'] == 'completed'
            await eventually(done)
            events = (await client.get('/chats/demo/events')).json()['events']
            assert all(e['bot_id'] == 'CastleBot' for e in events)
            assert len({e['sequence'] for e in events}) == len(events)
            assert any(e['type'] == 'dependency_decision_request' for e in events)
            assert len([e for e in events if e['type'] == 'agent_answer' and e['task_id'] == jack]) == 1


@pytest.mark.parametrize('output', ['not JSON', '{"action":"new_task","target_task_ids":["bad"],"reason":"x"}',
    '{"action":"follow_up","target_task_ids":["other-chat"],"reason":"x"}'])
async def test_router_malformed_and_unknown_references(db, output):
    gateway = SimpleNamespace(generate=AsyncMock(return_value=output))
    router = OpenAIRouter(gateway, db[2])
    result = await router.route({'sender': 'alice', 'new_message': '@CastleBot hi', 'tasks': [], 'history': []})
    assert result.action == 'clarification'
    assert gateway.generate.call_args.kwargs['schema']['additionalProperties'] is False


async def test_router_owner_validation_and_fenced_fallback(db):
    valid = '```json\n{"action":"follow_up","target_task_ids":["a"],"reason":"x"}\n```'
    assert parse_route_decision(valid).action == 'follow_up'
    router = OpenAIRouter(SimpleNamespace(generate=AsyncMock(return_value=valid)), db[2])
    result = await router.route({'sender': 'bob', 'tasks': [{'task_id': 'a', 'user_id': 'alice'}]})
    assert result.action == 'clarification'


async def test_executor_passes_actual_inputs_and_no_internal_token(db):
    gateway = SimpleNamespace(generate=AsyncMock(return_value='Complete useful answer'))
    executor = OpenAIExecutor(gateway, db[2])
    data = {'instructions': 'compare', 'prerequisites': [{'result': 'actual saved data', 'version': 2}],
            'conversation': [], 'run_token': 'private-control-token'}
    assert await executor.execute(data) == 'Complete useful answer'
    kwargs = gateway.generate.call_args.kwargs
    assert kwargs['data']['prerequisites'][0]['result'] == 'actual saved data'
    assert 'run_token' not in kwargs['data']
    assert 'no browser' in kwargs['system']


async def test_rate_limit_retries_are_bounded(db):
    response = httpx.Response(429, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    error = RateLimitError('rate limited', response=response, body=None)
    call = AsyncMock(side_effect=[error, error, 'success'])
    assert await with_retries(call, db[2]) == 'success'
    assert call.await_count == 3
    exhausted = AsyncMock(side_effect=error)
    with pytest.raises(RateLimitError):
        await with_retries(exhausted, db[2])
    assert exhausted.await_count == 3


async def test_nontransient_errors_are_not_retried(db):
    response = httpx.Response(400, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    call = AsyncMock(side_effect=BadRequestError('invalid', response=response, body=None))
    with pytest.raises(BadRequestError):
        await with_retries(call, db[2])
    assert call.await_count == 1



def mock_response(text='answer', status='completed', block_type='output_text', phase='final_answer'):
    return SimpleNamespace(status=status, output=[SimpleNamespace(type='message', phase=phase,
        content=[SimpleNamespace(type=block_type, text=text)])])


async def test_gateway_responses_schema_budget_and_incomplete(db):
    from executor import IncompleteResponseError
    gateway = OpenAIGateway(db[2])
    create = AsyncMock(return_value=mock_response())
    gateway.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    assert await gateway.generate(model='test', system='system', data={'instructions': 'request'}, max_tokens=700,
        schema=RouteDecision.model_json_schema(), reasoning_effort='none') == 'answer'
    kwargs = create.call_args.kwargs
    assert kwargs['instructions'] == 'system'
    assert json.loads(kwargs['input']) == {'instructions': 'request'}
    assert kwargs['text']['format']['strict'] is True
    assert kwargs['text']['format']['schema']['additionalProperties'] is False
    assert kwargs['reasoning'] == {'effort': 'none'}
    assert kwargs['max_output_tokens'] == 700
    assert kwargs['store'] is False and kwargs['service_tier'] == 'default'
    assert 'temperature' not in kwargs
    create.return_value = mock_response('truncated answer', status='incomplete')
    with pytest.raises(IncompleteResponseError):
        await gateway.generate(model='test', system='system', data={}, max_tokens=128)


async def test_gateway_refusal_empty_output_and_commentary(db):
    from executor import RefusalError
    gateway = OpenAIGateway(db[2])
    create = AsyncMock(return_value=mock_response(block_type='refusal'))
    gateway.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(RefusalError):
        await gateway.generate(model='test', system='system', data={}, max_tokens=700)
    create.return_value = mock_response('thinking aloud', phase='commentary')
    with pytest.raises(ValueError, match='no text'):
        await gateway.generate(model='test', system='system', data={}, max_tokens=700)
    result = mock_response('final')
    result.output.insert(0, mock_response('commentary', phase='commentary').output[0])
    create.return_value = result
    assert await gateway.generate(model='test', system='system', data={}, max_tokens=700) == 'final'


async def test_gateway_does_not_retry_incompatible_schema(db):
    gateway = OpenAIGateway(db[2])
    response = httpx.Response(400, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    create = AsyncMock(side_effect=BadRequestError('unsupported schema', response=response, body=None))
    gateway.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(BadRequestError):
        await gateway.generate(model='test', system='system', data={}, max_tokens=700, schema={'type': 'object'})
    assert create.await_count == 1


@pytest.mark.parametrize('code', ['insufficient_quota', 'credit_balance_exhausted',
    'organization_spend_limit_exceeded', 'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'])
async def test_insufficient_quota_not_retried(db, code):
    response = httpx.Response(429, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    call = AsyncMock(side_effect=RateLimitError('quota', response=response, body={'code': code}))
    with pytest.raises(RateLimitError):
        await with_retries(call, db[2])
    assert call.await_count == 1


@pytest.mark.parametrize('action,targets', [('new_task', []), ('follow_up', ['a']),
    ('dependent_task', ['a', 'b']), ('clarification', [])])
async def test_all_router_actions_use_openai_schema(db, action, targets):
    gateway = SimpleNamespace(generate=AsyncMock(return_value=json.dumps(
        {'action': action, 'target_task_ids': targets, 'reason': 'classification'})))
    decision = await OpenAIRouter(gateway, db[2]).route({'sender': 'alice', 'tasks': [
        {'task_id': 'a', 'user_id': 'alice'}, {'task_id': 'b', 'user_id': 'bob'}]})
    assert decision.action == action and decision.target_task_ids == targets
    assert gateway.generate.call_args.kwargs['reasoning_effort'] == 'none'


async def test_real_sdk_request_shape_with_mock_transport(db):
    """Validate the installed official SDK's JSON transport without any network."""
    from openai import AsyncOpenAI
    captured = []
    async def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'resp_mock', 'object': 'response', 'created_at': 1,
            'model': 'gpt-6-luna', 'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'id': 'msg_mock', 'status': 'completed',
                 'content': [{'type': 'output_text', 'text': '{"action":"new_task","target_task_ids":[],"reason":"New request"}', 'annotations': []}]}]})
    client = AsyncOpenAI(api_key='test-key-not-a-secret', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    gateway = OpenAIGateway(db[2])
    gateway.client = client
    try:
        decision = await OpenAIRouter(gateway, db[2]).route({'sender': 'alice', 'tasks': []})
        assert decision.action == 'new_task'
        assert captured[0]['model'] == db[2].router_model
        assert captured[0]['text']['format']['name'] == 'route_decision'
    finally:
        await gateway.close()
