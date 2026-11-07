"""Explicit demo-mock chats: deterministic routing and fixture answers via real orchestration.

No credentials or model calls are made for these chat IDs. The normal chat path
remains the configured OpenAI router/executor. This module never changes DB state.
"""
import asyncio

from config import Settings
from crud import Store
from executor import Executor
from router import Router
from schemas import RouteDecision

PREFIX = 'demo-mock-'


class MockRouter:
    async def route(self, context: dict) -> RouteDecision:
        content = context['new_message'].lower()
        tasks = context['tasks']
        own = [task for task in tasks if task['user_id'] == context['sender']]
        if 'can you update that earlier thing' in content:
            return RouteDecision(action='clarification', target_task_ids=[],
                reason='Which earlier task did you mean? I can update your StudyBuddy tagline or your pitch outline.')
        if 'use alice' in content and 'bob' in content and 'pitch' in content:
            alice = next((t for t in reversed(tasks) if t['user_id'] == 'alice' and 'feature' in t['content'].lower()), None)
            bob = next((t for t in reversed(tasks) if t['user_id'] == 'bob' and 'launch' in t['content'].lower()), None)
            if alice and bob:
                return RouteDecision(action='dependent_task', target_task_ids=[alice['task_id'], bob['task_id']],
                                     reason='The pitch needs Alice’s features and Bob’s launch ideas.')
        if 'update my earlier feature suggestions' in content or 'revise my feature' in content:
            task = next((t for t in reversed(own) if 'feature' in t['content'].lower()), None)
            if task:
                return RouteDecision(action='follow_up', target_task_ids=[task['task_id']], reason='Revise the feature request.')
        if 'update my studybuddy tagline task' in content or 'update my pitch outline task' in content:
            phrase = 'tagline' if 'tagline task' in content else 'outline'
            task = next((t for t in reversed(own) if phrase in t['content'].lower()), None)
            if task:
                return RouteDecision(action='follow_up', target_task_ids=[task['task_id']], reason='Clarified task revision.')
        return RouteDecision(action='new_task', target_task_ids=[], reason='Independent request.')


class MockExecutor:
    def __init__(self, delay_seconds: float = 2.8):
        self.delay_seconds = delay_seconds

    async def execute(self, inputs: dict) -> str:
        await asyncio.sleep(self.delay_seconds)
        content = inputs['instructions'].lower()
        parents = inputs['prerequisites']
        if parents:
            findings = '\n'.join(f"{p['user_id']} (v{p['version']}): {p['result']}" for p in parents)
            return ("Meet StudyBuddy: a calmer way to prepare for exams together. Students get a focused revision plan, "
                    "small group sessions, and a clear view of what to tackle next. We can launch on campus with "
                    "student ambassadors and low-cost study pop-ups. In 45 seconds, we’ll show how one busy week "
                    "becomes a manageable plan.\n\nBuilt from the selected findings:\n" + findings)
        if 'exam preparation' in content or 'revision planning' in content:
            return ('**Three exam-season features**\n\n1. A revision planner that turns exam dates into daily goals.\n'
                    '2. Shared study sessions with friends and gentle reminders.\n'
                    '3. A progress view that highlights topics needing another pass.')
        if 'core features' in content:
            return ('**Three core StudyBuddy features**\n\n1. A flexible study timetable for busy weeks.\n'
                    '2. Shared course goals and study sessions.\n3. A simple progress tracker with gentle reminders.')
        if 'low-budget' in content or 'launch ideas' in content:
            return ('**Three campus launch ideas**\n\n1. Recruit course reps as student ambassadors.\n'
                    '2. Host short study-planning pop-ups during exam season.\n'
                    '3. Share student-made demo clips in existing campus groups.')
        if 'checklist' in content:
            return '**Launch checklist**\n\n' + '\n'.join(f'{i}. Step {i}: coordinate the campus pilot.' for i in range(1, 11))
        if 'tagline' in content:
            return '**Tagline:** StudyBuddy makes tomorrow feel manageable.'
        if 'outline' in content:
            return '**Pitch outline:** Problem, plan, shared progress, campus launch.'
        return 'Here is a concise starting point for your StudyBuddy presentation.'


class DemoRouter:
    def __init__(self, live: Router):
        self.live = live
        self.mock = MockRouter()

    async def route(self, context: dict) -> RouteDecision:
        if context['chat_id'].startswith(PREFIX):
            return await self.mock.route(context)
        return await self.live.route(context)


class DemoExecutor:
    def __init__(self, live: Executor, settings: Settings, store: Store | None = None):
        self.live = live
        self.mock = MockExecutor()
        self.settings = settings
        self.store = store

    async def execute(self, inputs: dict) -> str:
        chat_id = inputs['chat_id']
        if (self.store and chat_id.startswith(('demo-mock-before-', 'demo-live-before-'))
                and inputs['version'] == 1 and inputs['prerequisites']
                and '45-second pitch' in inputs['instructions'].lower()):
            # Only the scripted first pitch waits. Its real run token is invalidated
            # when the prerequisite revision creates a version decision.
            while not await self.store.prerequisite_revised(chat_id, inputs['prerequisites']):
                await asyncio.sleep(0.15)
            if not await self.store.run_active((inputs['task_id'], inputs['version']), inputs['run_token']):
                raise asyncio.CancelledError
        if chat_id.startswith(PREFIX):
            return await self.mock.execute(inputs)
        if chat_id.startswith('demo-live-') and self.settings.demo_execution_delay_seconds:
            await asyncio.sleep(self.settings.demo_execution_delay_seconds)
        return await self.live.execute(inputs)
