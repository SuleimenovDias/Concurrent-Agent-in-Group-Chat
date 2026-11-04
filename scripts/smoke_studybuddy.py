"""One bounded live StudyBuddy run on a fresh temporary database; opt in with --live.

At most eight expected Responses calls (four routes, four executions), hard-capped at
15 attempts. Retries are disabled. Existing development data is untouched.
"""
import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from config import Settings
from database import Base, make_database
from demo_mock import DemoExecutor, DemoRouter
from executor import OpenAIExecutor, OpenAIGateway, safe_error
from main import create_app
from router import OpenAIRouter
import models  # noqa: F401


class CountedGateway:
    def __init__(self, delegate: OpenAIGateway):
        self.delegate = delegate
        self.requests = 0

    async def generate(self, **kwargs):
        if self.requests >= 15:
            raise RuntimeError('Live demo request cap reached')
        self.requests += 1
        return await self.delegate.generate(**kwargs)


class CapturingExecutor:
    def __init__(self, delegate):
        self.delegate = delegate
        self.inputs = []

    async def execute(self, inputs):
        self.inputs.append(inputs)
        return await self.delegate.execute(inputs)


async def wait_for(check, label, timeout=120):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.2)
    raise RuntimeError('Timed out waiting for ' + label)


async def run() -> bool:
    base = Settings()
    if not base.openai_api_key:
        print('Live test unavailable: OPENAI_API_KEY is not configured.')
        return False
    with TemporaryDirectory(prefix='castle_live_studybuddy_') as directory:
        db_url = 'sqlite+aiosqlite:///' + str(Path(directory) / 'demo.sqlite')
        settings = base.model_copy(update={'database_url': db_url, 'max_retries': 0,
            'router_max_output_tokens': 512, 'max_output_tokens': 1024,
            'scheduler_interval_seconds': 0.05, 'demo_execution_delay_seconds': 6})
        engine, sessions = make_database(db_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        gateway = OpenAIGateway(settings)
        counted = CountedGateway(gateway)
        executor = CapturingExecutor(DemoExecutor(OpenAIExecutor(counted, settings), settings))
        app = create_app(settings, sessions, engine,
                         router=DemoRouter(OpenAIRouter(counted, settings)), executor=executor)
        chat = 'demo-live-' + uuid4().hex[:12]
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    async def send(user, content):
                        response = await client.post('/messages', json={'chat_id': chat, 'user_id': user, 'content': content})
                        response.raise_for_status()
                        message_id = response.json()['message_id']
                        if '@CastleBot' not in content:
                            return None

                        async def routed():
                            message = (await client.get('/messages/' + message_id)).json()
                            if message['routing_status'] in {'failed', 'clarification'}:
                                raise RuntimeError('Live router returned ' + message['routing_status'])
                            return message['task_id']

                        return await wait_for(routed, 'routing')

                    await send('alice', "Morning guys! We should probably finalize what we're showing tomorrow.")
                    await send('bob', 'Agreed. We have the idea, but not the pitch yet.')
                    await send('jack', "I can put the pitch together once we settle the features.")
                    alice = await send('alice', '@CastleBot suggest three core features for StudyBuddy, a study-planning app for university students. Keep it brief.')
                    bob = await send('bob', '@CastleBot give us three low-budget ideas for launching StudyBuddy on campus.')
                    if not alice or not bob or alice == bob:
                        raise RuntimeError('Independent tasks were not created')
                    print('Alice and Bob received separate tasks.', flush=True)
                    jack = await send('jack', "@CastleBot use Alice's feature suggestions and Bob's launch ideas to write a 45-second pitch for our presentation.")
                    pitch = (await client.get('/tasks/' + jack)).json()
                    if {d['prerequisite_task_id'] for d in pitch['dependencies']} != {alice, bob}:
                        raise RuntimeError('Jack was not linked to both prerequisite tasks')
                    print('Jack received both task dependencies.', flush=True)
                    revision = await send('alice', '@CastleBot update my earlier feature suggestions to focus on exam preparation and revision planning rather than everyday scheduling.')
                    if revision != alice or (await client.get('/tasks/' + alice)).json()['version'] != 2:
                        raise RuntimeError('Alice revision was not saved as version 2')

                    async def decision():
                        ds = (await client.get('/chats/' + chat + '/decisions')).json()
                        return ds[0] if ds else None

                    pending = await wait_for(decision, 'version choice')
                    chosen = await client.post('/dependency-decisions/' + pending['decision_id'] + '/resolve',
                                               json={'user_id': 'jack', 'choice': 'use_updated'})
                    chosen.raise_for_status()
                    print('Jack selected Alice version 2.', flush=True)

                    async def done():
                        tasks = [(await client.get('/tasks/' + task_id)).json() for task_id in (alice, bob, jack)]
                        failed = [t for t in tasks if t['status'] in {'failed', 'cancelled'}]
                        if failed:
                            raise RuntimeError('Live task did not complete: ' + '; '.join(str(t['error']) for t in failed))
                        return tasks if all(t['status'] == 'completed' and t['result'] for t in tasks) else None

                    tasks = await wait_for(done, 'published results', timeout=180)
                    parent = next(d for d in tasks[2]['dependencies'] if d['prerequisite_task_id'] == alice)
                    if parent['prerequisite_version'] != 2:
                        raise RuntimeError('Jack did not use the selected version')
                    pitch_inputs = [i for i in executor.inputs if i['instructions'].lower().find('45-second pitch') >= 0]
                    if not pitch_inputs or len(pitch_inputs[-1]['prerequisites']) != 2:
                        raise RuntimeError('The pitch executor did not receive saved prerequisite results')
                    events = (await client.get('/chats/' + chat + '/events?limit=200')).json()['events']
                    if len([e for e in events if e['type'] == 'agent_answer' and e['task_id'] == jack]) != 1:
                        raise RuntimeError('The final pitch was not published exactly once')
                    print(f'PASS: real OpenAI StudyBuddy run; {counted.requests} model requests; '
                          f'{len(events)} ordered events; final pitch published once.', flush=True)
                    return True
        except Exception as exc:
            print(f'Live StudyBuddy run failed after {counted.requests} model requests: {safe_error(exc)}', flush=True)
            return False
        finally:
            await gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow at most 15 paid OpenAI request attempts')
    if not parser.parse_args().live:
        parser.error('--live is required')
    raise SystemExit(0 if asyncio.run(run()) else 1)
