import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../src/App'
import { api, type Event, type Message, type Task } from '../src/api'

const stamp = '2026-11-04T10:00:00Z'
const messages: Message[] = [
  {message_id: 'a', chat_id: 'demo-mock-test', user_id: 'alice', content: 'Morning guys!', task_id: null, sequence: 1, routing_status: 'not_needed', created_at: stamp},
  {message_id: 'b', chat_id: 'demo-mock-test', user_id: 'bob', content: 'Agreed.', task_id: null, sequence: 2, routing_status: 'not_needed', created_at: stamp},
  {message_id: 'j', chat_id: 'demo-mock-test', user_id: 'jack', content: 'I can write the pitch.', task_id: null, sequence: 3, routing_status: 'not_needed', created_at: stamp},
]
const answer: Event = {event_id: 'answer-1', chat_id: 'demo-mock-test', sequence: 4, type: 'agent_answer', bot_id: 'CastleBot', task_id: 'task-1', version: 1, message_id: null, payload: {content: '**A good pitch** for StudyBuddy.'}, created_at: stamp}

beforeEach(() => {
  vi.spyOn(api, 'config').mockResolvedValue({mock_available: true, live_available: false, live_delay_seconds: 0})
  vi.spyOn(api, 'messages').mockResolvedValue(messages)
  vi.spyOn(api, 'events').mockResolvedValue({events: [answer], next_cursor: 4})
  vi.spyOn(api, 'tasks').mockResolvedValue([])
  vi.spyOn(api, 'decisions').mockResolvedValue([])
  vi.spyOn(api, 'send').mockResolvedValue({message_id: 'saved', status: 'stored_no_action'})
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('phone chat', () => {
  it('shows its controls inside the phone and renders each published message once', async () => {
    const user = userEvent.setup()
    render(<App />)
    expect(screen.getByRole('heading', {name: 'StudyBuddy Team'})).toBeInTheDocument()
    expect(screen.getByRole('button', {name: 'Start Demo'})).toBeInTheDocument()
    await user.click(screen.getByRole('button', {name: 'Demo menu'}))
    expect(screen.getByRole('region', {name: 'Demo controls'})).toBeInTheDocument()
    expect(screen.getByRole('button', {name: 'Live OpenAI'})).toBeDisabled()
    await user.click(screen.getByRole('button', {name: 'Close menu'}))
    await user.click(screen.getByRole('button', {name: 'Start Demo'}))
    await waitFor(() => expect(screen.getAllByTestId('chat-message')).toHaveLength(4))
    expect(screen.getByText('Morning guys!')).toBeInTheDocument()
    expect(screen.getByText('Agreed.')).toBeInTheDocument()
    expect(screen.getByText('I can write the pitch.')).toBeInTheDocument()
    expect(screen.getAllByText(/A good pitch/)).toHaveLength(1)
    expect(within(screen.getAllByTestId('chat-message')[3]).getByText(/StudyBuddy/)).toBeInTheDocument()
  })

  it('shows a connection error without claiming a bot response', async () => {
    vi.spyOn(api, 'messages').mockRejectedValue(new Error('Backend unavailable'))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', {name: 'Start Demo'}))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Backend unavailable'))
    expect(screen.queryByText(/A good pitch/)).not.toBeInTheDocument()
  })

  it('shows separate progress for independent work and a waiting pitch', async () => {
    const task = (task_id: string, user_id: string, status: string): Task => ({task_id, user_id, status,
      chat_id: 'demo-mock-test', version: 1, content: '', result: null, error: null, dependencies: [], pending_decisions: []})
    vi.spyOn(api, 'tasks').mockResolvedValue([task('t1','alice','running'), task('t2','bob','running'),
      task('t3','jack','waiting_for_dependencies')])
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', {name: 'Start Demo'}))
    await waitFor(() => expect(screen.getByText('CastleBot is working on 2 requests…')).toBeInTheDocument())
    expect(screen.getByText('Alice’s features · writing')).toBeInTheDocument()
    expect(screen.getByText('Bob’s launch ideas · writing')).toBeInTheDocument()
    expect(screen.getByText('Jack’s pitch · waiting on results')).toBeInTheDocument()
  })

  it('shows the avatar and source reply for each consecutive CastleBot event', async () => {
    const decision: Event = {...answer, event_id: 'decision-1', sequence: 4, type: 'dependency_decision_request',
      message_id: 'j', payload: {decision_id: 'd1', question: 'Which version should I use?'}}
    vi.spyOn(api, 'events').mockResolvedValue({events: [decision, {...answer, sequence: 5, message_id: 'a'}], next_cursor: 5})
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', {name: 'Start Demo'}))
    await waitFor(() => expect(screen.getAllByTestId('chat-message')).toHaveLength(5))
    const botRows = screen.getAllByTestId('chat-message').filter(row => row.dataset.sender === 'castlebot')
    expect(botRows).toHaveLength(2)
    for (const row of botRows) expect(within(row).getByLabelText('CastleBot')).toBeInTheDocument()
    expect(within(botRows[0]).getByRole('button', {name: "Jump to Jack's message"})).toBeInTheDocument()
    const source = document.getElementById('message-a')!
    const jump = vi.spyOn(source, 'scrollIntoView')
    await user.click(within(botRows[1]).getByRole('button', {name: "Jump to Alice's message"}))
    expect(jump).toHaveBeenCalledWith({behavior: 'smooth', block: 'center'})
  })

  it('offers regeneration when a dependent answer was already published', async () => {
    const decision = {decision_id: 'choice-1', task_id: 'task-1', version: 1, prerequisite_task_id: 'alice-task',
      original_version: 1, updated_version: 2, status: 'pending', choice: null, answer_published: true}
    const question: Event = {...answer, event_id: 'question-1', sequence: 5, type: 'dependency_decision_request',
      payload: {...decision, question: 'Jack, your original result is complete and remains in the chat. Generate a revised result?'}}
    vi.spyOn(api, 'events').mockResolvedValue({events: [answer, question], next_cursor: 5})
    vi.spyOn(api, 'decisions').mockResolvedValue([decision])
    const resolve = vi.spyOn(api, 'resolve').mockResolvedValue({...decision, status: 'resolved', choice: 'use_updated'})
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', {name: 'Demo menu'}))
    await user.click(screen.getByRole('button', {name: /Revise after the pitch/}))
    await waitFor(() => expect(screen.getByRole('button', {name: 'Generate revised result'})).toBeInTheDocument())
    expect(screen.getByText(/original result is complete and remains in the chat/)).toBeInTheDocument()
    expect(screen.getByRole('button', {name: 'Keep completed result'})).toBeInTheDocument()
    await user.click(screen.getByRole('button', {name: 'Generate revised result'}))
    expect(resolve).toHaveBeenCalledWith('choice-1', 'use_updated')
  })
})
