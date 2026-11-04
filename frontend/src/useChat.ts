import { useEffect, useState } from 'react'
import { api, type Decision, type Event, type Message, type Task } from './api'

export interface ChatData { messages: Message[]; events: Event[]; tasks: Task[]; decisions: Decision[]; error: string | null; connected: boolean }
const empty: ChatData = { messages: [], events: [], tasks: [], decisions: [], error: null, connected: false }

export function useChat(chatId: string | null): ChatData {
  const [data, setData] = useState<ChatData>(empty)
  useEffect(() => {
    setData(empty)
    if (!chatId) return
    let cancelled = false
    let messageCursor = 0
    let eventCursor = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    let unresolved = new Map<string, Message>()
    async function poll() {
      try {
        // A cursor advances only after the page has been merged into local state.
        const [messages, page, tasks, decisions, refreshed] = await Promise.all([
          api.messages(chatId!, messageCursor, controller.signal), api.events(chatId!, eventCursor, controller.signal),
          api.tasks(chatId!, controller.signal), api.decisions(chatId!, controller.signal),
          Promise.all([...unresolved.keys()].map(id => api.message(id, controller.signal)))
        ])
        if (cancelled) return
        setData(prev => {
          const allMessages = new Map(prev.messages.map(m => [m.message_id, m]))
          for (const m of [...messages, ...refreshed]) allMessages.set(m.message_id, m)
          const allEvents = new Map(prev.events.map(e => [e.event_id, e]))
          for (const e of page.events) allEvents.set(e.event_id, e)
          return { messages: [...allMessages.values()].sort((a,b) => a.sequence-b.sequence),
            events: [...allEvents.values()].sort((a,b) => a.sequence-b.sequence),
            tasks, decisions, error: null, connected: true }
        })
        messageCursor = messages.at(-1)?.sequence ?? messageCursor
        unresolved = new Map([...messages, ...refreshed].filter(m => ['pending','routing'].includes(m.routing_status)).map(m => [m.message_id, m]))
        eventCursor = page.next_cursor
        timer = setTimeout(poll, messages.length === 200 || page.events.length === 200 ? 0 : 650)
      } catch (error) {
        if (cancelled) return
        setData(prev => ({...prev, connected: false, error: error instanceof Error ? error.message : 'Connection lost'}))
        timer = setTimeout(poll, 1600)
      }
    }
    void poll()
    return () => { cancelled = true; controller.abort(); if (timer) clearTimeout(timer) }
  }, [chatId])
  return data
}
