import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowLeft, Check, CheckCheck, ChevronDown, CircleHelp, Ellipsis, Pause, Play, RotateCcw, Send, Signal, Sparkles, StepForward, Wifi, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type Config, type DemoMode, type Event, type Message, type Participant, type Scenario } from './api'
import { scenarios } from './scenarios'
import { useChat } from './useChat'
import { useDemo } from './useDemo'
import './styles.css'

const names: Record<Participant | 'castlebot', string> = {alice: 'Alice', bob: 'Bob', jack: 'Jack', castlebot: 'CastleBot'}
const initials: Record<Participant | 'castlebot', string> = {alice: 'A', bob: 'B', jack: 'J', castlebot: '✦'}
const avatarClass: Record<Participant | 'castlebot', string> = {alice: 'alice', bob: 'bob', jack: 'jack', castlebot: 'castlebot'}
const time = (date: string) => new Intl.DateTimeFormat('en', {hour: 'numeric', minute: '2-digit'}).format(new Date(date))

type TimelineItem = {key: string; at: string; rank: number; sender: Participant | 'castlebot'; message?: Message; event?: Event}

function Avatar({person, size = 'regular'}: {person: Participant | 'castlebot'; size?: 'regular' | 'small'}) {
  return <span className={`avatar avatar--${avatarClass[person]} avatar--${size}`} aria-label={names[person]}>{initials[person]}</span>
}

function BotMarkdown({children}: {children: string}) {
  return <div className="bot-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>
}

function DecisionButtons({published, busy, choose}: {published: boolean; busy: boolean; choose: (choice: 'keep_original' | 'use_updated') => void}) {
  return <div className="decision-buttons"><button disabled={busy} onClick={() => choose('keep_original')}>{published ? 'Keep completed result' : 'Use original version'}</button><button disabled={busy} onClick={() => choose('use_updated')}>{published ? 'Generate revised result' : 'Use updated version'} <ArrowDown size={13}/></button></div>
}

function App() {
  const demo = useDemo()
  const {state} = demo
  const {messages, events, tasks, decisions, error: feedError, connected} = useChat(state.chatId)
  const [config, setConfig] = useState<Config | null>(null)
  const [menu, setMenu] = useState(false)
  const [draft, setDraft] = useState('')
  const [sender, setSender] = useState<Participant>('alice')
  const [manualError, setManualError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const [nearBottom, setNearBottom] = useState(true)
  const userScroll = useRef(false)

  useEffect(() => { api.config().then(setConfig).catch(() => setConfig({mock_available: true, live_available: false, live_delay_seconds: 0})) }, [])
  useEffect(() => { userScroll.current = false; setNearBottom(true) }, [state.chatId])
  useEffect(() => { if (nearBottom) endRef.current?.scrollIntoView({behavior: 'smooth', block: 'end'}) }, [messages, events, tasks, nearBottom])
  const latestDecisionEvents = useMemo(() => {
    const ids = new Map<string, number>()
    for (const event of events) if (event.type === 'dependency_decision_request') ids.set(String(event.payload.decision_id), event.sequence)
    return ids
  }, [events])
  const messagesById = useMemo(() => new Map(messages.map(message => [message.message_id, message])), [messages])
  const timeline = useMemo<TimelineItem[]>(() => {
    const fromMessages: TimelineItem[] = messages.map(m => ({key: m.message_id, at: m.created_at, rank: m.sequence,
      sender: m.user_id as Participant, message: m}))
    const fromEvents: TimelineItem[] = events.filter(e => ['agent_answer','clarification_request','failure_notification','dependency_decision_request'].includes(e.type))
      .filter(e => e.type !== 'dependency_decision_request' || latestDecisionEvents.get(String(e.payload.decision_id)) === e.sequence)
      .map(e => ({key: e.event_id, at: e.created_at, rank: e.sequence, sender: 'castlebot', event: e}))
    return [...fromMessages, ...fromEvents].sort((a,b) => a.at.localeCompare(b.at) || (a.message ? 0 : 1) - (b.message ? 0 : 1) || a.rank-b.rank)
  }, [messages, events, latestDecisionEvents])
  const running = tasks.filter(t => t.status === 'running').length
  const waiting = tasks.filter(t => t.status === 'waiting_for_dependencies').length
  const routing = messages.filter(m => ['pending','routing'].includes(m.routing_status)).length
  const activeTasks = tasks.filter(t => ['queued','running','waiting_for_dependencies','waiting_for_decision'].includes(t.status))
  const taskLabel = (task: typeof tasks[number]) => {
    const subject = task.user_id === 'alice' ? 'features' : task.user_id === 'bob' ? 'launch ideas' : task.user_id === 'jack' ? 'pitch' : 'request'
    const status = task.status === 'running' ? 'writing' : task.status === 'queued' ? 'queued' :
      task.status === 'waiting_for_decision' ? 'awaiting a choice' : 'waiting on results'
    return `${names[task.user_id as Participant] ?? task.user_id}’s ${subject} · ${status}`
  }
  const activity = running > 1 ? `CastleBot is working on ${running} requests…` :
    running === 1 ? 'CastleBot is working on a request…' :
    waiting > 0 ? 'CastleBot is waiting for Alice’s and Bob’s results.' :
    routing > 0 ? 'CastleBot is reading the conversation…' : null
  const canCompose = state.phase === 'done' || state.phase === 'idle' || state.phase === 'paused'
  const resolved = new Map(events.filter(e => e.type === 'task_status' && typeof e.payload.decision_id === 'string')
    .map(e => [String(e.payload.decision_id), String(e.payload.choice)]))
  const scenario = scenarios[state.scenario]

  async function submitManual(event: React.FormEvent) {
    event.preventDefault()
    const content = draft.trim()
    if (!content || sending) return
    setSending(true); setManualError(null)
    try { await demo.manualSend(sender, content); setDraft('') }
    catch (error) { setManualError(error instanceof Error ? error.message : 'Could not send message.') }
    finally { setSending(false) }
  }
  function switchScenario(next: Scenario) { setMenu(false); void demo.start(false, next, state.mode) }
  function switchMode(next: DemoMode) { setMenu(false); void demo.start(false, state.scenario, next) }

  return <div className="stage">
    <div className="ambient ambient--one"/><div className="ambient ambient--two"/>
    <main className="phone" aria-label="StudyBuddy group chat demo">
      <div className="phone-screen">
        <div className="system-bar" aria-hidden="true"><span>9:41</span><span className="island"/><span className="system-icons"><Signal size={16}/><Wifi size={16}/><span className="battery"><span/></span></span></div>
        <header className="chat-header">
          <button className="icon-button back-button" aria-label="Back to chat" onClick={() => setMenu(false)}><ArrowLeft size={21}/></button>
          <div className="group-avatar"><span className="group-avatar__spark"><Sparkles size={18}/></span><span className="group-avatar__small">✦</span></div>
          <div className="header-copy"><h1>StudyBuddy Team</h1><p><span className="online-dot"/> Alice, Bob, Jack &amp; CastleBot</p></div>
          <button className={`icon-button menu-toggle ${menu ? 'is-open' : ''}`} aria-label="Demo menu" aria-expanded={menu} onClick={() => setMenu(!menu)}><Ellipsis size={25}/></button>
        </header>
        {menu && <div className="menu-shade" onClick={() => setMenu(false)}><section className="demo-menu" onClick={e => e.stopPropagation()} aria-label="Demo controls">
          <div className="menu-head"><div><span className="menu-eyebrow">CASTLEBOT DEMO</span><h2>Make it yours</h2></div><button className="icon-button" aria-label="Close menu" onClick={() => setMenu(false)}><X size={18}/></button></div>
          <p className="menu-label">Scenario</p>
          <div className="scenario-list">{(Object.keys(scenarios) as Scenario[]).map(id => <button key={id} className={`scenario-option ${state.scenario === id ? 'selected' : ''}`} onClick={() => switchScenario(id)}><span><strong>{scenarios[id].title}</strong><small>{scenarios[id].caption}</small></span>{state.scenario === id && <Check size={17}/>}</button>)}</div>
          <p className="menu-label">CastleBot replies</p>
          <div className="mode-toggle"><button className={state.mode === 'mock' ? 'active' : ''} onClick={() => switchMode('mock')}>Mock preview</button><button className={state.mode === 'live' ? 'active' : ''} disabled={!config?.live_available} onClick={() => switchMode('live')}>Live OpenAI</button></div>
          {!config?.live_available && <p className="menu-note">Set OPENAI_API_KEY in FastAPI to enable live replies.</p>}
          <div className="menu-actions"><button onClick={() => {setMenu(false); void demo.start(true)}}><Play size={16}/> Start Demo</button><button onClick={() => {setMenu(false); state.phase === 'playing' ? demo.pause() : demo.resume()}} disabled={!state.chatId || state.phase === 'decision' || state.phase === 'done'}><Pause size={16}/> {state.phase === 'playing' ? 'Pause' : 'Resume'}</button><button onClick={() => {setMenu(false); void demo.advance()}} disabled={!state.chatId || state.busy || state.phase === 'done'}><StepForward size={16}/> Next Message</button><button onClick={() => {setMenu(false); void demo.start(true)}}><RotateCcw size={16}/> Restart Demo</button></div>
          <p className="menu-note">Live mode calls your OpenAI account after you press Start Demo. Mock preview uses fixed examples.</p>
        </section></div>}
        <div className="chat-subheader"><span className={`mode-badge mode-badge--${state.mode}`}><span/>{state.mode === 'live' ? 'LIVE OPENAI' : 'MOCK PREVIEW'}</span><span>{state.chatId && !connected ? 'Connecting…' : scenario.title}<ChevronDown size={13}/></span></div>
        <div className="conversation" ref={scrollRef} onWheel={e => {const el = e.currentTarget; if (e.deltaY < 0 || el.scrollHeight-el.scrollTop-el.clientHeight >= 120) userScroll.current = true}} onTouchMove={() => {userScroll.current = true}} onScroll={e => {if (!userScroll.current) return; const el = e.currentTarget; const bottom = el.scrollHeight-el.scrollTop-el.clientHeight < 120; setNearBottom(bottom); if (bottom) userScroll.current = false}}>
          {!state.chatId && <div className="intro"><div className="intro-icon"><Sparkles size={28}/></div><div className="intro-rule"/><span className="intro-eyebrow">WELCOME TO THE GROUP</span><h2>Good ideas are<br/><em>better together.</em></h2><p>Watch three friends plan their big pitch with a little help from CastleBot.</p><div className="intro-members"><Avatar person="alice"/><Avatar person="bob"/><Avatar person="jack"/><Avatar person="castlebot"/></div><button className="start-button" onClick={() => void demo.start(true)}><Play size={17} fill="currentColor"/> Start Demo</button><span className="intro-hint">Select a scenario in the ••• menu</span></div>}
          {state.chatId && <><div className="date-divider"><span>Today</span></div><div className="scene-caption"><Sparkles size={12}/> {scenario.caption}</div>
            {timeline.map((item, index) => {
              const own = item.sender === sender
              const previous = timeline[index - 1]
              const grouped = item.sender !== 'castlebot' && !!previous && previous.sender === item.sender && new Date(item.at).getTime() - new Date(previous.at).getTime() < 90_000
              const name = names[item.sender] ?? item.sender
              const event = item.event
              const source = event?.message_id ? messagesById.get(event.message_id) : undefined
              const decisionId = event?.type === 'dependency_decision_request' ? String(event.payload.decision_id) : null
              const pending = decisionId ? decisions.find(d => d.decision_id === decisionId) ?? (state.decision?.decision_id === decisionId ? state.decision : null) : null
              const published = decisionId ? pending?.answer_published ?? (typeof event?.payload.answer_published === 'boolean'
                ? event.payload.answer_published : events.some(e => e.type === 'agent_answer' && e.task_id === event?.task_id && e.version === event?.version && e.sequence < event.sequence)) : false
              const question = published && event?.payload.answer_published !== true
                ? 'The original result is already complete and remains in the chat. Keep it or generate a revised result using the updated task?'
                : String(event?.payload.question ?? 'Which version should I use?')
              return <div key={item.key} id={item.message ? `message-${item.message.message_id}` : undefined} className={`message-row ${own ? 'message-row--own' : ''} ${grouped ? 'message-row--grouped' : ''}`} data-testid="chat-message" data-sender={item.sender}>
                {!own && <div className="message-avatar">{!grouped && <Avatar person={item.sender}/>}</div>}
                <div className={`bubble bubble--${item.sender}`}>
                  {!own && !grouped && <span className="sender-name">{name}</span>}
                  {source && <button type="button" className="reply-preview" aria-label={`Jump to ${names[source.user_id as Participant] ?? source.user_id}'s message`} onClick={() => {userScroll.current = true; setNearBottom(false); document.getElementById(`message-${source.message_id}`)?.scrollIntoView({behavior: 'smooth', block: 'center'})}}><span className="reply-preview__sender">{names[source.user_id as Participant] ?? source.user_id}</span><span className="reply-preview__text">{source.content}</span></button>}
                  {item.message ? <p className="bubble-text">{item.message.content}</p> : event?.type === 'agent_answer' ? <BotMarkdown>{String(event.payload.content ?? '')}</BotMarkdown> :
                   event?.type === 'dependency_decision_request' ? <><p className="bubble-text">{question}</p>
                    {pending ? <DecisionButtons published={published} busy={state.busy || state.phase === 'playing'} choose={choice => void demo.choose(choice, pending)}/> : resolved.has(decisionId!) ? <span className="choice-note">{published ? resolved.get(decisionId!) === 'use_updated' ? 'Revised result requested' : 'Completed result kept' : resolved.get(decisionId!) === 'use_updated' ? 'Updated version selected' : 'Original version selected'}</span> : null}</> :
                   event?.type === 'clarification_request' ? <><p className="bubble-text">{String(event.payload.question ?? 'Which task did you mean?')}</p>{state.scenario === 'ambiguity' && <div className="decision-buttons"><button onClick={() => {setSender('alice'); setDraft('@CastleBot update my StudyBuddy tagline task to focus on exam season.')}}>Tagline</button><button onClick={() => {setSender('alice'); setDraft('@CastleBot update my pitch outline task to focus on exam season.')}}>Pitch outline</button></div>}</> :
                   <p className="bubble-text error-text">{String(event?.payload.error ?? 'CastleBot could not finish this request.')}</p>}
                  <div className="bubble-meta"><time>{time(item.at)}</time>{own && <CheckCheck size={13}/>}</div>
                </div>
              </div>
            })}
            {activity && <div className="activity"><div className="activity-heading"><span className="activity-icon"><Sparkles size={14}/></span><span>{activity}</span>{running > 0 && <span className="typing-dots"><i/><i/><i/></span>}</div>{activeTasks.length > 0 && <div className="activity-tasks">{activeTasks.map(task => <span key={task.task_id}>{taskLabel(task)}</span>)}</div>}</div>}
            {state.notice && <div className="notice" role="status"><Check size={13}/>{state.notice}</div>}
            {(state.error || manualError || feedError) && <div className="error-notice" role="alert"><CircleHelp size={15}/><span>{state.error ?? manualError ?? feedError}</span><button aria-label="Dismiss error" onClick={() => setManualError(null)}><X size={13}/></button></div>}
            {state.phase === 'decision' && !events.some(e => e.type === 'dependency_decision_request' && e.payload.decision_id === state.decision?.decision_id) && <div className="decision-pending"><Avatar person="castlebot"/><div><p>{state.decision?.answer_published ? 'The original result is complete and remains in the chat. Keep it or generate a revised result?' : 'Alice updated her feature suggestions. Jack, which version should I use?'}</p><DecisionButtons published={Boolean(state.decision?.answer_published)} busy={state.busy} choose={choice => void demo.choose(choice)}/></div></div>}
            {state.phase === 'done' && <div className="end-caption">You’re all caught up <Sparkles size={13}/></div>}
            <div ref={endRef}/></>}
        </div>
        {state.chatId && <div className="playback-strip"><span className="playback-progress">{Math.min(state.step, scenario.steps.length)} <span>/ {scenario.steps.length}</span></span><div className="progress-track"><span style={{width: `${Math.min(100, state.step / scenario.steps.length * 100)}%`}}/></div><span className="playback-state">{state.phase === 'decision' ? 'Your choice' : state.phase === 'done' ? 'Finished' : state.phase === 'paused' ? 'Paused' : state.phase === 'error' ? 'Needs attention' : 'Playing'}</span><button aria-label={state.phase === 'playing' ? 'Pause demo' : 'Resume demo'} disabled={state.phase === 'decision' || state.phase === 'done' || !state.chatId} onClick={state.phase === 'playing' ? demo.pause : demo.resume}>{state.phase === 'playing' ? <Pause size={15}/> : <Play size={15}/>}</button><button aria-label="Next message" disabled={state.busy || state.phase === 'done'} onClick={() => void demo.advance()}><StepForward size={16}/></button></div>}
        <form className="composer" onSubmit={submitManual}><div className="participant-picker"><Avatar person={sender} size="small"/><select value={sender} aria-label="Send as participant" onChange={e => setSender(e.target.value as Participant)}><option value="alice">Alice</option><option value="bob">Bob</option><option value="jack">Jack</option></select></div><input value={draft} onChange={e => setDraft(e.target.value)} placeholder={canCompose ? 'Message StudyBuddy Team' : 'Demo is playing…'} disabled={!canCompose} aria-label="Message"/><button type="submit" aria-label="Send message" disabled={!draft.trim() || sending || !canCompose}><Send size={17}/></button></form>
        <div className="home-indicator" aria-hidden="true"/>
      </div>
    </main>
  </div>
}
export default App
