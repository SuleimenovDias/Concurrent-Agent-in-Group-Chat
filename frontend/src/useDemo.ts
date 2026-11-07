import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Decision, type DemoMode, type Participant, type Scenario, type Task } from './api'
import { scenarios, type Step } from './scenarios'

export type DemoPhase = 'idle' | 'playing' | 'paused' | 'decision' | 'done' | 'error'
export interface DemoState {
  chatId: string | null; scenario: Scenario; mode: DemoMode; phase: DemoPhase; step: number
  busy: boolean; error: string | null; decision: Decision | null; notice: string | null
  refs: Partial<Record<'alice' | 'bob' | 'jack' | 'revision', string>>
}
const initial: DemoState = {chatId: null, scenario: 'main', mode: 'mock', phase: 'idle', step: 0,
  busy: false, error: null, decision: null, notice: null, refs: {}}
const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export function useDemo() {
  const [state, setState] = useState<DemoState>(initial)
  const current = useRef(state)
  const generation = useRef(0)
  const lock = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const set = useCallback((patch: Partial<DemoState>) => {
    current.current = {...current.current, ...patch}
    setState(current.current)
  }, [])
  const active = (epoch: number) => epoch === generation.current
  const until = useCallback(async <T,>(epoch: number, load: () => Promise<T>, check: (item: T) => boolean, label: string): Promise<T> => {
    const deadline = Date.now() + 90000
    while (active(epoch) && Date.now() < deadline) {
      const item = await load()
      if (!active(epoch)) throw new Error('Run changed')
      if (check(item)) return item
      await wait(280)
    }
    throw new Error(`Timed out waiting for ${label}. Check the backend events.`)
  }, [])
  const start = useCallback(async (autoplay = true, scenario = current.current.scenario, mode = current.current.mode) => {
    const epoch = ++generation.current
    if (timer.current) clearTimeout(timer.current)
    const old = current.current
    // Cancellation is best effort and never deletes old conversations.
    if (old.chatId) {
      for (const id of Object.values(old.refs)) {
        try { const task = await api.task(id); if (['queued','running','waiting_for_dependencies','waiting_for_decision'].includes(task.status)) await api.cancel(id) }
        catch { /* The old chat remains inspectable. */ }
      }
    }
    if (!active(epoch)) return
    const timing = scenario === 'main' ? 'before-' : scenario === 'revision_after' ? 'after-' : ''
    const chatId = `demo-${mode}-${timing}${crypto.randomUUID()}`
    set({chatId, scenario, mode, phase: autoplay ? 'playing' : 'paused', step: 0, busy: false,
      decision: null, error: null, notice: null, refs: {}})
  }, [set])
  const choose = useCallback(async (choice: 'keep_original' | 'use_updated', offered?: Decision) => {
    const snapshot = current.current
    const decision = snapshot.decision ?? offered
    if (!decision || !snapshot.chatId || lock.current) return
    const epoch = generation.current
    lock.current = true
    set({busy: true, error: null})
    try {
      const result = await api.resolve(decision.decision_id, choice)
      if (!active(epoch)) return
      if (result.status !== 'resolved') throw new Error('The decision was not accepted.')
      set({decision: null, phase: snapshot.phase === 'decision' ? 'playing' : snapshot.phase, step: snapshot.step, busy: false,
        notice: decision.answer_published
          ? choice === 'use_updated' ? 'Jack requested a revised result.' : 'Jack kept the completed result.'
          : choice === 'use_updated' ? 'Jack chose the updated feature suggestions.' : 'Jack kept the original feature suggestions.'})
    } catch (error) {
      if (active(epoch)) set({busy: false, error: error instanceof Error ? error.message : 'Could not save choice'})
    } finally { lock.current = false }
  }, [set])
  const perform = useCallback(async (step: Step, epoch: number) => {
    const snapshot = current.current
    const chatId = snapshot.chatId!
    const refs = snapshot.refs
    if (step.kind === 'message') {
      const accepted = await api.send(chatId, step.sender, step.content)
      if (!step.content.toLowerCase().includes('@castlebot')) return
      const routed = await until(epoch, () => api.message(accepted.message_id),
        m => !!m.task_id || ['clarification','failed'].includes(m.routing_status), 'CastleBot routing')
      if (routed.routing_status === 'failed') throw new Error('CastleBot could not route that message. See the failure in chat.')
      if (routed.routing_status === 'clarification') {
        if (snapshot.scenario === 'ambiguity') return
        throw new Error('CastleBot needs a clearer task reference. See its question in chat.')
      }
      const task = await api.task(routed.task_id!)
      if (step.expect === 'independent' && task.dependencies.length) throw new Error('The backend did not create an independent task.')
      if (step.expect === 'dependent' && task.dependencies.length !== 2) throw new Error('The pitch was not linked to both earlier tasks.')
      if (step.expect === 'revision' && (task.version < 2 || task.task_id !== refs.alice)) throw new Error('Alice’s follow-up was not saved as a new version.')
      if (step.ref) {
        if (step.ref === 'bob' && snapshot.scenario === 'main' && task.task_id === refs.alice) throw new Error('Two requests received one task ID.')
        set({refs: {...current.current.refs, [step.ref]: task.task_id}})
      }
      return
    }
    if (step.kind === 'cancel') {
      const taskId = refs.bob
      if (!taskId) throw new Error('No checklist task was created.')
      try {
        const task = await api.cancel(taskId)
        set({notice: task.status === 'cancelled' ? 'CastleBot stopped the checklist request.' : `Checklist status: ${task.status}.`})
      } catch (error) {
        const task = await api.task(taskId)
        set({notice: task.status === 'completed' ? 'The checklist had already finished before Bob changed course.' :
          `Cancellation was not accepted: ${error instanceof Error ? error.message : 'unknown error'}.`})
      }
      return
    }
    if (step.kind === 'await-decision') {
      const prior = await api.events(chatId, 0)
      if (prior.events.some(e => e.type === 'task_status' && e.task_id === refs.jack &&
          typeof e.payload.decision_id === 'string')) return
      const decisions = await until(epoch, () => api.decisions(chatId), ds => ds.some(d => d.task_id === refs.jack), 'Jack’s version choice')
      const decision = decisions.find(d => d.task_id === refs.jack)!
      set({decision, phase: 'decision', notice: null})
      return
    }
    if (step.kind === 'await-answer') {
      const taskId = refs[step.ref]
      if (!taskId) throw new Error('The pitch task was not created.')
      await until(epoch, () => api.events(chatId, 0), page => page.events.some(e =>
        e.type === 'agent_answer' && e.task_id === taskId && e.version === 1), 'Jack’s first published answer')
      return
    }
    if (step.kind === 'await-pitch') {
      const outcome = await until(epoch, async () => ({pitch: await api.task(refs.jack!), page: await api.events(chatId, 0)}),
        ({pitch, page}) => ['failed','cancelled'].includes(pitch.status) ||
          (pitch.status === 'completed' && page.events.some(e => e.type === 'agent_answer' && e.task_id === pitch.task_id && e.version === pitch.version)),
        'Jack’s published pitch')
      const pitch: Task = outcome.pitch
      if (pitch.status !== 'completed') throw new Error(`Jack’s pitch ${pitch.status}: ${pitch.error ?? 'inspect task status'}`)
      return
    }
    if (step.kind === 'await-clarification') {
      // Accept the router's real outcome. It can clarify or link a task; the UI reports whichever happened.
      const messages = await api.messages(chatId, 0)
      const last = messages.at(-1)
      if (!last) throw new Error('The ambiguous request was not saved.')
      if (!['clarification','routed','failed'].includes(last.routing_status))
        await until(epoch, () => api.message(last.message_id), m => ['clarification','routed','failed'].includes(m.routing_status), 'routing outcome')
      return
    }
  }, [set, until])
  const advance = useCallback(async () => {
    const snapshot = current.current
    if (!snapshot.chatId || lock.current) return
    if (snapshot.phase === 'decision') { await choose('use_updated'); return }
    const steps = scenarios[snapshot.scenario].steps
    if (snapshot.step >= steps.length) return
    lock.current = true
    const epoch = generation.current
    set({busy: true, error: null})
    try {
      await perform(steps[snapshot.step], epoch)
      if (!active(epoch)) return
      const nextStep = snapshot.step + 1
      set({step: nextStep, busy: false, phase: current.current.phase === 'decision' ? 'decision' :
        nextStep >= steps.length ? 'done' : current.current.phase === 'paused' ? 'paused' : 'playing'})
    } catch (error) {
      if (active(epoch)) set({busy: false, phase: 'error', error: error instanceof Error ? error.message : 'Demo step failed'})
    } finally { lock.current = false }
  }, [choose, perform, set])
  useEffect(() => {
    if (state.phase !== 'playing' || state.busy || !state.chatId) return
    const step = scenarios[state.scenario].steps[state.step]
    if (!step) return
    timer.current = setTimeout(() => void advance(), step.delay)
    return () => { if (timer.current) clearTimeout(timer.current) }
  }, [state.phase, state.busy, state.step, state.chatId, state.scenario, advance])
  const pause = () => set({phase: 'paused'})
  const resume = () => set({phase: 'playing', error: null})
  const manualSend = async (sender: Participant, content: string) => {
    let chatId = current.current.chatId
    if (!chatId) {
      chatId = `demo-${current.current.mode}-${crypto.randomUUID()}`
      set({chatId, phase: 'done'})
    }
    await api.send(chatId, sender, content)
  }
  return {state, start, pause, resume, advance, choose, manualSend}
}
