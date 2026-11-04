"""Opt-in real OpenAI smoke test. Creates a unique chat; never deletes data.

Run from the repository root: python -m scripts.smoke_demo --live
The --live switch makes the paid network calls explicit.
"""
import argparse
import asyncio
from uuid import uuid4

import httpx

from config import Settings
from database import make_database
from executor import OpenAIExecutor, OpenAIGateway
from main import create_app


class GatedExecutor:
    """Hold the first requests so the revision/decision race is reproducible."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.release = asyncio.Event()
        self.started = asyncio.Queue()
        self.active = 0
        self.maximum = 0

    async def execute(self, inputs):
        await self.started.put(inputs['instructions'])
        await self.release.wait()
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            return await self.delegate.execute(inputs)
        finally:
            self.active -= 1


async def run(choice: str) -> None:
    settings = Settings(max_output_tokens=512)
    engine, sessions = make_database(settings.database_url)
    gateway = OpenAIGateway(settings)
    executor = GatedExecutor(OpenAIExecutor(gateway, settings))
    app = create_app(settings, sessions, engine, executor=executor)
    chat = 'smoke-' + uuid4().hex[:12]
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://smoke') as client:
                async def send(user, content):
                    response = await client.post('/messages', json={'chat_id': chat, 'user_id': user,
                        'content': '@CastleBot ' + content + ' Answer in at most 100 words.'})
                    response.raise_for_status()
                    message_id = response.json()['message_id']
                    async with asyncio.timeout(120):
                        while True:
                            message = (await client.get('/messages/' + message_id)).json()
                            if message['routing_status'] in {'failed', 'clarification'}:
                                events = (await client.get(f'/chats/{chat}/events?limit=200')).json()['events']
                                detail = next((e['payload'].get('error') or e['payload'].get('question')
                                    for e in events if e['message_id'] == message_id and
                                    e['type'] in {'failure_notification', 'clarification_request'}), 'Inspect chat events')
                                raise RuntimeError('Routing did not create the expected task: ' + detail)
                            if message['task_id']:
                                return message['task_id']
                            await asyncio.sleep(0.1)
                alice = await send('alice', "explain NVIDIA's business model from general knowledge")
                bob = await send('bob', "explain AMD's business model from general knowledge")
                await asyncio.wait_for(executor.started.get(), 120)
                await asyncio.wait_for(executor.started.get(), 120)
                print('Alice and Bob started concurrently.', flush=True)
                jack = await send('jack', "compare Alice's and Bob's findings")
                assert (await client.get('/tasks/' + jack)).json()['status'] == 'waiting_for_dependencies'
                assert await send('alice', "actually focus my NVIDIA task on NVIDIA's data-center business") == alice
                decisions = (await client.get(f'/chats/{chat}/decisions')).json()
                assert len(decisions) == 1
                response = await client.post('/dependency-decisions/' + decisions[0]['decision_id'] + '/resolve',
                    json={'user_id': 'jack', 'choice': choice})
                response.raise_for_status()
                executor.release.set()
                print('Dependency decision resolved: ' + choice, flush=True)
                async with asyncio.timeout(180):
                    while True:
                        tasks = [(await client.get('/tasks/' + task_id)).json() for task_id in [alice, bob, jack]]
                        if any(t['status'] in {'failed', 'cancelled'} for t in tasks):
                            errors = [t['error'] for t in tasks if t['status'] in {'failed', 'cancelled'}]
                            raise RuntimeError('Task failed: ' + str(errors))
                        if all(t['status'] == 'completed' and t['result'] for t in tasks):
                            break
                        await asyncio.sleep(0.2)
                jack_task = tasks[2]
                selected = next(d for d in jack_task['dependencies'] if d['prerequisite_task_id'] == alice)
                assert selected['prerequisite_version'] == (1 if choice == 'keep_original' else 2)
                events = (await client.get(f'/chats/{chat}/events?limit=200')).json()['events']
                assert len({e['sequence'] for e in events}) == len(events)
                assert len([e for e in events if e['type'] == 'agent_answer' and e['task_id'] == jack]) == 1
                assert not any(e['type'] == 'agent_answer' and e['task_id'] == alice and e['version'] == 1 for e in events)
                assert executor.maximum >= 2
                print(f'PASS: {chat}; {len(events)} ordered events; peak concurrent OpenAI executions={executor.maximum}.')
                print('All current tasks completed with real OpenAI results; Jack used the selected saved prerequisites.')
    finally:
        await gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Acknowledge real, billable OpenAI calls')
    parser.add_argument('--choice', choices=['keep_original', 'use_updated'], default='keep_original')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; automated tests use mocks instead')
    asyncio.run(run(args.choice))
