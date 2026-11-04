"""Demo routes use the real scheduler and database, with no paid model requests."""
import asyncio

import httpx

from main import create_app


async def wait_for(check, timeout=4):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError('Timed out waiting for demo state')


async def test_mock_studybuddy_concurrency_revision_and_publication(db):
    engine, sessions, settings = db
    app = create_app(settings, sessions, engine)
    app.state.scheduler.executor.mock.delay_seconds = 0.7
    chat = 'demo-mock-integration'
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def send(user, content):
                response = await client.post('/messages', json={'chat_id': chat, 'user_id': user, 'content': content})
                assert response.status_code == 202
                return response.json()

            ordinary = await send('alice', 'Morning guys!')
            assert ordinary['status'] == 'stored_no_action'
            assert (await client.get('/chats/' + chat + '/tasks')).json() == []

            alice_m = await send('alice', '@CastleBot suggest three core features for StudyBuddy. Keep it brief.')
            bob_m = await send('bob', '@CastleBot give us three low-budget ideas for launching StudyBuddy on campus.')

            async def both_running():
                tasks = (await client.get('/chats/' + chat + '/tasks')).json()
                return tasks if len(tasks) == 2 and all(t['status'] == 'running' for t in tasks) else None

            tasks = await wait_for(both_running)
            assert len({t['task_id'] for t in tasks}) == 2
            alice = (await client.get('/messages/' + alice_m['message_id'])).json()['task_id']
            bob = (await client.get('/messages/' + bob_m['message_id'])).json()['task_id']
            assert alice != bob

            jack_m = await send('jack', "@CastleBot use Alice's feature suggestions and Bob's launch ideas to write a 45-second pitch.")

            async def jack_waiting():
                message = (await client.get('/messages/' + jack_m['message_id'])).json()
                if not message['task_id']:
                    return None
                task = (await client.get('/tasks/' + message['task_id'])).json()
                return task if task['status'] == 'waiting_for_dependencies' else None

            jack = await wait_for(jack_waiting)
            assert {d['prerequisite_task_id'] for d in jack['dependencies']} == {alice, bob}

            revision_m = await send('alice', '@CastleBot update my earlier feature suggestions to focus on exam preparation and revision planning rather than everyday scheduling.')

            async def pending_decision():
                ds = (await client.get('/chats/' + chat + '/decisions')).json()
                return ds[0] if ds else None

            decision = await wait_for(pending_decision)
            revision = (await client.get('/messages/' + revision_m['message_id'])).json()
            assert revision['task_id'] == alice
            assert (await client.get('/tasks/' + alice)).json()['version'] == 2
            assert (await client.get('/tasks/' + alice + '/versions/1')).json()['version'] == 1
            resolved = await client.post('/dependency-decisions/' + decision['decision_id'] + '/resolve',
                                         json={'user_id': 'jack', 'choice': 'use_updated'})
            assert resolved.status_code == 200 and resolved.json()['status'] == 'resolved'

            async def pitch_done():
                task = (await client.get('/tasks/' + jack['task_id'])).json()
                return task if task['status'] == 'completed' else None

            final = await wait_for(pitch_done)
            assert next(d for d in final['dependencies'] if d['prerequisite_task_id'] == alice)['prerequisite_version'] == 2
            assert 'exam' in final['result'].lower()
            page = (await client.get('/chats/' + chat + '/events')).json()
            events = page['events']
            answers = [e for e in events if e['type'] == 'agent_answer']
            assert len({e['event_id'] for e in answers}) == len(answers)
            assert len([e for e in answers if e['task_id'] == jack['task_id']]) == 1
            assert (await client.get(f"/chats/{chat}/events?after={page['next_cursor']}")).json()['events'] == []


async def test_mock_cancellation_and_ambiguity(db):
    engine, sessions, settings = db
    app = create_app(settings, sessions, engine)
    app.state.scheduler.executor.mock.delay_seconds = 0.6
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def submit(chat, content):
                response = await client.post('/messages', json={'chat_id': chat, 'user_id': 'alice', 'content': content})
                assert response.status_code == 202
                return response.json()['message_id']

            cancelled_chat = 'demo-mock-cancel'
            message_id = await submit(cancelled_chat, '@CastleBot create a detailed 10-step checklist for launching StudyBuddy.')

            async def checklist_running():
                m = (await client.get('/messages/' + message_id)).json()
                if not m['task_id']:
                    return None
                task = (await client.get('/tasks/' + m['task_id'])).json()
                return task if task['status'] == 'running' else None

            checklist = await wait_for(checklist_running)
            response = await client.post('/tasks/' + checklist['task_id'] + '/cancel')
            assert response.status_code == 200 and response.json()['status'] == 'cancelled'
            assert not any(e['type'] == 'agent_answer' for e in (await client.get('/chats/' + cancelled_chat + '/events')).json()['events'])

            ambiguous_chat = 'demo-mock-ambiguous'
            await submit(ambiguous_chat, '@CastleBot suggest a tagline for StudyBuddy.')
            await submit(ambiguous_chat, '@CastleBot outline a short pitch for StudyBuddy.')
            vague = await submit(ambiguous_chat, '@CastleBot can you update that earlier thing?')

            async def clarified():
                m = (await client.get('/messages/' + vague)).json()
                return m if m['routing_status'] == 'clarification' else None

            assert (await wait_for(clarified))['task_id'] is None
            events = (await client.get('/chats/' + ambiguous_chat + '/events')).json()['events']
            assert any(e['type'] == 'clarification_request' for e in events)
