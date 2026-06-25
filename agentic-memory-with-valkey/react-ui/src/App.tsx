import { useState, useRef, useEffect } from 'react'
import Chat from './components/Chat'
import type { ChatHandle } from './components/Chat'
import HumanChatPanel from './components/HumanChatPanel'
import ConversationSidebar from './components/ConversationSidebar'
import type { HumanChatStatus } from './components/HumanChatPanel'
import { strandsToUIMessages } from './utils/messageConverter'
import './App.css'

function App() {
  const [humanChatStatus, setHumanChatStatus] = useState<HumanChatStatus | null>(null)
  const connectionTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const titleFlashIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const originalTitleRef = useRef<string>(document.title)
  const [viewingHumanChat, setViewingHumanChat] = useState<boolean>(false)
  const [hasUnreadHumanMessages, setHasUnreadHumanMessages] = useState<boolean>(false)

  // Semantic cache feature flag — 'off' | 'hot' | 'new'
  const [fullCacheMode, setFullCacheMode] = useState<'off' | 'hot' | 'new'>('off')
  const [kbCacheMode, setKbCacheMode] = useState<'off' | 'hot' | 'new'>('off')
  const [fullCacheThreshold, setFullCacheThreshold] = useState<number>(0.65)
  const [kbCacheThreshold, setKbCacheThreshold] = useState<number>(0.70)

  // Memory system state
  const [userId, setUserId] = useState<string>('')
  const [shortTermMemoryEnabled, setShortTermMemoryEnabled] = useState<boolean>(true)
  const [longTermMemoryEnabled, setLongTermMemoryEnabled] = useState<boolean>(false)

  // Conversation management
  const [conversationIds, setConversationIds] = useState<string[]>([])
  const [activeConversationId, setActiveConversationId] = useState<string>('')
  const [conversationNames, setConversationNames] = useState<Map<string, string>>(new Map())

  // Session resumption — refs for Chat handles and pending message injection
  const chatRefs = useRef<Map<string, ChatHandle>>(new Map())
  const pendingRestoreRef = useRef<Map<string, any[]>>(new Map())
  const restoredSessionIds = useRef<Set<string>>(new Set())

  useEffect(() => {
    const firstId = `session-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`
    setConversationIds([firstId])
    setActiveConversationId(firstId)
    setConversationNames(new Map([[firstId, 'New chat']]))
  }, [])

  useEffect(() => { return () => { stopTitleFlash() } }, [])

  const handleConnectToHuman = () => {
    setHumanChatStatus('connecting')
    connectionTimeoutRef.current = setTimeout(() => {
      setHumanChatStatus('connected')
      connectionTimeoutRef.current = null
    }, 5000)
  }

  const handleEndChat = () => {
    if (connectionTimeoutRef.current) {
      clearTimeout(connectionTimeoutRef.current)
      connectionTimeoutRef.current = null
    }
    setHumanChatStatus(null)
    setViewingHumanChat(false)
    setHasUnreadHumanMessages(false)
    stopTitleFlash()
  }

  const handleNewChat = () => {
    const newId = `session-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`
    setConversationIds((prev) => [newId, ...prev])
    setActiveConversationId(newId)
    setConversationNames((prev) => new Map(prev).set(newId, 'New chat'))
  }

  const handleSelectConversation = (id: string) => {
    setActiveConversationId(id)
    setViewingHumanChat(false)

    // If this session has pending restored messages, inject them after the Chat mounts
    if (pendingRestoreRef.current.has(id)) {
      const msgs = pendingRestoreRef.current.get(id)!
      pendingRestoreRef.current.delete(id)
      setTimeout(() => {
        const chatHandle = chatRefs.current.get(id)
        if (chatHandle && msgs.length > 0) {
          chatHandle.setMessages(msgs)
        }
      }, 100)
    }
  }

  // When a known user is selected from the dropdown, fetch their past sessions
  // and add them directly into the conversations list (no separate "past" section)
  const handleUserSelected = async (selectedUserId: string) => {
    if (!selectedUserId.trim()) return

    // Clear previously restored sessions from the sidebar
    if (restoredSessionIds.current.size > 0) {
      const toRemove = restoredSessionIds.current
      setConversationIds((prev) => prev.filter((id) => !toRemove.has(id)))
      setConversationNames((prev) => {
        const next = new Map(prev)
        toRemove.forEach((id) => next.delete(id))
        return next
      })
      // Clean up refs
      toRemove.forEach((id) => {
        pendingRestoreRef.current.delete(id)
        chatRefs.current.delete(id)
      })
      restoredSessionIds.current = new Set()
    }

    try {
      const res = await fetch(`/api/user-sessions/${encodeURIComponent(selectedUserId)}`)
      const data = await res.json()
      const sessions = (data.sessions || []) as Array<{ session_id: string; last_active: number; name: string }>
      if (sessions.length === 0) return

      const existing = new Set(conversationIds)
      const newSessions = sessions.filter((s) => !existing.has(s.session_id))
      if (newSessions.length === 0) return

      // Fetch messages for all sessions in parallel
      const loaded = await Promise.all(
        newSessions.map(async (s) => {
          try {
            const msgRes = await fetch(`/api/session-messages/${encodeURIComponent(s.session_id)}`)
            const msgData = await msgRes.json()
            return { ...s, uiMessages: strandsToUIMessages(msgData.messages || []) }
          } catch {
            return { ...s, uiMessages: [] as any[] }
          }
        })
      )

      // Stash converted messages for injection when the user clicks a session.
      // API returns most-recent-first. Append them after existing conversations
      // so the sidebar's [...].reverse() renders them newest-first above older ones.
      const newIds: string[] = []
      const newNames = new Map<string, string>()
      for (const s of loaded) {
        newIds.push(s.session_id)
        newNames.set(s.session_id, s.name || 'Restored session')
        if (s.uiMessages.length > 0) {
          pendingRestoreRef.current.set(s.session_id, s.uiMessages)
        }
      }

      // newIds is newest-first from API. Append after existing conversations
      // so "New chat" stays at top and restored sessions appear below, newest first.
      newIds.forEach((id) => restoredSessionIds.current.add(id))
      setConversationIds((prev) => [...prev, ...newIds])
      setConversationNames((prev) => {
        const next = new Map(prev)
        newNames.forEach((v, k) => next.set(k, v))
        return next
      })
    } catch (e) {
      console.error('Failed to fetch user sessions:', e)
    }
  }

  const startTitleFlash = () => {
    if (titleFlashIntervalRef.current) clearInterval(titleFlashIntervalRef.current)
    let isFlashing = false
    titleFlashIntervalRef.current = setInterval(() => {
      document.title = isFlashing ? originalTitleRef.current : '💬 New message!'
      isFlashing = !isFlashing
    }, 1000)
  }

  const stopTitleFlash = () => {
    if (titleFlashIntervalRef.current) {
      clearInterval(titleFlashIntervalRef.current)
      titleFlashIntervalRef.current = null
    }
    document.title = originalTitleRef.current
  }

  const handleSelectHumanChat = () => {
    setViewingHumanChat(true)
    setHasUnreadHumanMessages(false)
    stopTitleFlash()
  }

  const handleNewHumanMessage = () => {
    if (!viewingHumanChat) {
      setHasUnreadHumanMessages(true)
      startTitleFlash()
    }
  }

  const handleConversationNamed = (id: string, name: string) => {
    setConversationNames((prev) => new Map(prev).set(id, name))
  }

  return (
    <div className="app-container">
      <ConversationSidebar
        conversationIds={conversationIds}
        activeConversationId={activeConversationId}
        conversationNames={conversationNames}
        onSelectConversation={handleSelectConversation}
        onNewChat={handleNewChat}
        showHumanChat={humanChatStatus !== null}
        hasUnreadHumanMessages={hasUnreadHumanMessages}
        onSelectHumanChat={handleSelectHumanChat}
        viewingHumanChat={viewingHumanChat}
        fullCacheMode={fullCacheMode}
        onFullCacheModeChange={setFullCacheMode}
        fullCacheThreshold={fullCacheThreshold}
        onFullCacheThresholdChange={setFullCacheThreshold}
        kbCacheMode={kbCacheMode}
        onKbCacheModeChange={setKbCacheMode}
        kbCacheThreshold={kbCacheThreshold}
        onKbCacheThresholdChange={setKbCacheThreshold}
        userId={userId}
        onUserIdChange={setUserId}
        shortTermMemoryEnabled={shortTermMemoryEnabled}
        onShortTermMemoryChange={setShortTermMemoryEnabled}
        longTermMemoryEnabled={longTermMemoryEnabled}
        onLongTermMemoryChange={setLongTermMemoryEnabled}
        onUserSelected={handleUserSelected}
      />
      <div className={`main-chat ${humanChatStatus ? 'with-panel' : ''}`}>
        {humanChatStatus && (
          <div className={`chat-wrapper ${viewingHumanChat ? 'active' : ''}`}>
            <HumanChatPanel
              status={humanChatStatus}
              onEndChat={handleEndChat}
              onNewHumanMessage={handleNewHumanMessage}
            />
          </div>
        )}
        {conversationIds.map((id) => (
          <div
            key={id}
            className={`chat-wrapper ${!viewingHumanChat && id === activeConversationId ? 'active' : ''}`}
          >
            <Chat
              ref={(handle) => {
                if (handle) chatRefs.current.set(id, handle)
                else chatRefs.current.delete(id)
              }}
              conversationId={id}
              onConnectToHuman={handleConnectToHuman}
              onConversationNamed={handleConversationNamed}
              cacheMode={fullCacheMode !== 'off' ? 'full' : kbCacheMode !== 'off' ? 'subagent' : 'off'}
              fullCacheTemp={fullCacheMode === 'new' ? 'cold' : 'hot'}
              kbCacheTemp={kbCacheMode === 'new' ? 'cold' : 'hot'}
              kbCacheEnabled={kbCacheMode !== 'off'}
              fullCacheThreshold={fullCacheThreshold}
              kbCacheThreshold={kbCacheThreshold}
              userId={longTermMemoryEnabled ? userId : undefined}
              shortTermMemoryEnabled={shortTermMemoryEnabled}
              longTermMemoryEnabled={longTermMemoryEnabled}
            />
          </div>
        ))}
      </div>
      {humanChatStatus && (
        <div className="side-panel">
          <HumanChatPanel
            status={humanChatStatus}
            onEndChat={handleEndChat}
            onNewHumanMessage={handleNewHumanMessage}
          />
        </div>
      )}
    </div>
  )
}

export default App
