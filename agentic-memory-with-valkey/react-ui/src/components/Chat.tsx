import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';
import { useState, useEffect, useRef, useImperativeHandle, forwardRef, FormEvent } from 'react';
import MessagePart from './MessagePart';
import './Chat.css';

// Helper: merge consecutive reasoning blocks, collect following tools, inject llm-call markers
function consolidateParts(parts: any[] | undefined): any[] {
  if (!parts || parts.length === 0) return [];

  // First pass: merge consecutive reasoning parts into single blocks
  const merged: any[] = [];
  for (const part of parts) {
    const last = merged[merged.length - 1];
    if (part.type === 'reasoning' && last?.type === 'reasoning') {
      last.text += part.text;
      if (part.state === 'streaming') last.state = 'streaming';
    } else {
      merged.push(part.type === 'reasoning' ? { ...part, tools: [] } : part);
    }
  }

  // Second pass: attach tool parts that immediately follow a reasoning block onto it
  const absorbed = new Set<number>();
  for (let i = 0; i < merged.length; i++) {
    if (merged[i].type === 'reasoning') {
      let j = i + 1;
      while (j < merged.length && merged[j].type?.startsWith('tool-')) {
        merged[i].tools.push(merged[j].type.slice(5));
        absorbed.add(j);
        j++;
      }
    }
  }

  // Third pass: inject llm-call markers based on turn boundaries.
  // A new LLM call starts at: (a) the very first non-data part, or
  // (b) the first reasoning/text/tool part after a tool-result (i.e. after a tool-* part).
  const result: any[] = [];
  let llmCallIndex = 0;
  let needMarker = true; // start with a marker for the first LLM output
  for (let i = 0; i < merged.length; i++) {
    if (absorbed.has(i)) continue;
    const p = merged[i];
    // Skip internal data parts for marker logic
    if (p.type?.startsWith('data-')) {
      result.push(p);
      continue;
    }
    // After a tool call, the next reasoning or text is a new LLM call
    if (p.type?.startsWith('tool-')) {
      result.push(p);
      needMarker = true;
      continue;
    }
    // Insert marker before reasoning or text when we need one
    if (needMarker && (p.type === 'reasoning' || p.type === 'text')) {
      llmCallIndex++;
      result.push({ type: 'llm-call', index: llmCallIndex });
      needMarker = false;
    }
    result.push(p);
  }

  return result;
}

interface ChatProps {
  onConnectToHuman: () => void;
  conversationId: string;
  onConversationStart?: (id: string) => void;
  onConversationNamed?: (id: string, name: string) => void;
  cacheMode?: 'off' | 'subagent' | 'full';
  fullCacheTemp?: 'hot' | 'cold';
  kbCacheTemp?: 'hot' | 'cold';
  kbCacheEnabled?: boolean;
  fullCacheThreshold?: number;
  kbCacheThreshold?: number;
  userId?: string;
  shortTermMemoryEnabled?: boolean;
  longTermMemoryEnabled?: boolean;
}

export interface ChatHandle {
  setMessages: (msgs: any[]) => void;
}

const Chat = forwardRef<ChatHandle, ChatProps>(function Chat({ onConnectToHuman, conversationId, onConversationStart, onConversationNamed, cacheMode = 'off', fullCacheTemp = 'hot', kbCacheTemp = 'hot', kbCacheEnabled = false, fullCacheThreshold = 0.88, kbCacheThreshold = 0.90, userId, shortTermMemoryEnabled = true, longTermMemoryEnabled = false }, ref) {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const [input, setInput] = useState('');
  const processedToolCallsRef = useRef<Set<string>>(new Set());
  const hasStartedRef = useRef<boolean>(false);
  const sendTimesRef = useRef<Map<number, number>>(new Map());
  const ttftCapturedRef = useRef<Set<number>>(new Set());  // track which assistant msgs have TTFT captured
  const responseEndTimeRef = useRef<number | null>(null);  // set by data-response-complete
  const [responseDurations, setResponseDurations] = useState<Map<number, number>>(new Map());
  const [ttftDurations, setTtftDurations] = useState<Map<number, number>>(new Map());
  const [cacheableFlags, setCacheableFlags] = useState<Map<number, {cacheable: boolean, reason: string}>>(new Map());
  const [pendingCacheable, setPendingCacheable] = useState<{cacheable: boolean, reason: string} | null>(null);
  const [memoryMetadata, setMemoryMetadata] = useState<Map<number, {total: number, short_term: number, long_term: number}>>(new Map());
  const [pendingMemory, setPendingMemory] = useState<{total: number, short_term: number, long_term: number} | null>(null);
  const [memoryStorageMetadata, setMemoryStorageMetadata] = useState<Map<number, {short_term: number, long_term: number}>>(new Map());
  const prevStatusRef = useRef<string>('');

  // Keep a ref to the latest cache props so the transport always sends current values.
  // body supports a resolver function — read the ref on every send so toggles are never stale.
  const cachePropRef = useRef({ cacheMode, fullCacheTemp, kbCacheTemp, kbCacheEnabled, fullCacheThreshold, kbCacheThreshold, userId, shortTermMemoryEnabled, longTermMemoryEnabled });
  cachePropRef.current = { cacheMode, fullCacheTemp, kbCacheTemp, kbCacheEnabled, fullCacheThreshold, kbCacheThreshold, userId, shortTermMemoryEnabled, longTermMemoryEnabled };

  // Poll /api/conversation-title after response completes — retries until title is ready or timeout
  const pollForTitle = (sessionId: string) => {
    const maxAttempts = 10;
    const intervalMs = 800;
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch(`/api/conversation-title/${sessionId}`);
        const data = await res.json();
        if (data.title) {
          clearInterval(interval);
          if (onConversationNamed) onConversationNamed(sessionId, data.title);
        }
      } catch { /* ignore */ }
      if (attempts >= maxAttempts) clearInterval(interval);
    }, intervalMs);
  };

  // Poll /api/memory-storage-status after response completes
  const pollForMemoryStorage = (sessionId: string, assistantMsgIdx: number) => {
    const maxAttempts = 15;
    const intervalMs = 1000;
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch(`/api/memory-storage-status/${sessionId}`);
        const data = await res.json();
        if (data.ready) {
          clearInterval(interval);
          const total = (data.short_term || 0) + (data.long_term || 0);
          if (total > 0) {
            setMemoryStorageMetadata(prev => new Map(prev).set(assistantMsgIdx, {
              short_term: data.short_term || 0,
              long_term: data.long_term || 0,
            }));
          }
        }
      } catch { /* ignore */ }
      if (attempts >= maxAttempts) clearInterval(interval);
    }, intervalMs);
  };

  const { messages, sendMessage, setMessages: setChatMessages, error, status } = useChat({
    id: conversationId,
    transport: new DefaultChatTransport({
      api: '/api/invocations',
      body: () => cachePropRef.current,
    }),
    onData: (data) => {
      if (data && typeof data === 'object') {
        const dataAny = data as any;
        if (dataAny.type === 'data-cacheable' && dataAny.data) {
          setPendingCacheable({
            cacheable: dataAny.data.cacheable,
            reason: dataAny.data.reason,
            cache_hit: dataAny.data.cache_hit || false,
            kb_cache_hit: dataAny.data.kb_cache_hit || false,
            kb_cache_similarity: dataAny.data.kb_cache_similarity || null,
          } as any);
        }
        if (dataAny.type === 'data-memory-retrieved' && dataAny.data) {
          // Store memory metadata - use a pending state that will be applied to the next assistant message
          console.log('[MEMORY UI] data-memory-retrieved event received:', {
            total: dataAny.data.total,
            short_term: dataAny.data.short_term,
            long_term: dataAny.data.long_term,
            messagesLength: messages.length,
          });
          setPendingMemory({
            total: dataAny.data.total,
            short_term: dataAny.data.short_term,
            long_term: dataAny.data.long_term,
          });
        }
        if (dataAny.type === 'data-response-complete') {
          responseEndTimeRef.current = Date.now();
          // Poll for title async — doesn't block response timing
          if (conversationId) {
            pollForTitle(conversationId);
          }
        }
      }
    },
  });

  const isLoading = status === 'streaming';

  // Expose setMessages to parent via ref
  useImperativeHandle(ref, () => ({
    setMessages: (msgs: any[]) => setChatMessages(msgs),
  }), [setChatMessages]);

  // Wrap sendMessage to always record send time
  const send = (msg: Parameters<typeof sendMessage>[0]) => {
    sendTimesRef.current.set(messages.length, Date.now());
    sendMessage(msg);
  };

  // Track when streaming finishes to apply cacheability flag, memory metadata, and duration
  useEffect(() => {
    if (prevStatusRef.current === 'streaming' && status !== 'streaming') {
      const lastUserIdx = [...messages].reverse().findIndex(m => m.role === 'user');
      if (lastUserIdx !== -1) {
        const userMsgIdx = messages.length - 1 - lastUserIdx;
        const assistantMsgIdx = userMsgIdx + 1;
        const sendTime = sendTimesRef.current.get(userMsgIdx);
        const endTime = responseEndTimeRef.current || Date.now();
        responseEndTimeRef.current = null;
        const duration = sendTime ? endTime - sendTime : 0;
        setResponseDurations(prev => new Map(prev).set(assistantMsgIdx, duration));
        if (pendingCacheable) {
          setCacheableFlags(prev => new Map(prev).set(assistantMsgIdx, pendingCacheable));
          setPendingCacheable(null);
        }
        if (pendingMemory) {
          console.log('[MEMORY UI] Applying pending memory to assistant message:', assistantMsgIdx, pendingMemory);
          setMemoryMetadata(prev => new Map(prev).set(assistantMsgIdx, pendingMemory));
          setPendingMemory(null);
        }
        // Start polling for memory storage results (fire-and-forget on backend)
        if (conversationId && (shortTermMemoryEnabled || longTermMemoryEnabled)) {
          pollForMemoryStorage(conversationId, assistantMsgIdx);
        }
      }
    }
    // Handle late-arriving pendingCacheable (state update processed after stream ended)
    if (status !== 'streaming' && prevStatusRef.current !== 'streaming' && pendingCacheable) {
      const lastUserIdx = [...messages].reverse().findIndex(m => m.role === 'user');
      if (lastUserIdx !== -1) {
        const userMsgIdx = messages.length - 1 - lastUserIdx;
        setCacheableFlags(prev => new Map(prev).set(userMsgIdx + 1, pendingCacheable));
        setPendingCacheable(null);
      }
    }
    // Handle late-arriving pendingMemory
    if (status !== 'streaming' && prevStatusRef.current !== 'streaming' && pendingMemory) {
      const lastUserIdx = [...messages].reverse().findIndex(m => m.role === 'user');
      if (lastUserIdx !== -1) {
        const userMsgIdx = messages.length - 1 - lastUserIdx;
        console.log('[MEMORY UI] Applying late pending memory to assistant message:', userMsgIdx + 1, pendingMemory);
        setMemoryMetadata(prev => new Map(prev).set(userMsgIdx + 1, pendingMemory));
        setPendingMemory(null);
      }
    }
    prevStatusRef.current = status;
  }, [status, messages, pendingCacheable, pendingMemory]);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Capture TTFT: when an assistant message first gets a text part with content, record the time
  useEffect(() => {
    if (status !== 'streaming') return;
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.role === 'assistant' && msg.parts && !ttftCapturedRef.current.has(i)) {
        // Only trigger on actual text content — not reasoning, not tool calls
        const hasText = msg.parts.some((p: any) => p.type === 'text' && p.text && p.text.trim().length > 0);
        if (!hasText) continue;
        ttftCapturedRef.current.add(i);
        const userMsgIdx = i - 1;
        const sendTime = sendTimesRef.current.get(userMsgIdx);
        if (sendTime) {
          const ttft = Date.now() - sendTime;
          setTtftDurations(prev => new Map(prev).set(i, ttft));
        }
        break;
      }
    }
  }, [messages, status]);

  // Reset hasStarted flag when conversation changes
  useEffect(() => {
    hasStartedRef.current = false;
  }, [conversationId]);

  // Notify parent when conversation starts (first message sent)
  useEffect(() => {
    if (messages.length > 0 && !hasStartedRef.current && onConversationStart) {
      hasStartedRef.current = true;
      onConversationStart(conversationId);
    }
  }, [messages, conversationId, onConversationStart]);



  // Detect connect_to_it_support_human tool calls
  useEffect(() => {
    for (const message of messages) {
      if (message.role === 'assistant' && message.parts) {
        for (const part of message.parts) {
          // Check for tool invocation with name "connect_to_it_support_human"
          if (
            (part.type === 'tool-invocation' || part.type === 'tool-connect_to_support_human') &&
            ((part as any).toolName === 'connect_to_support_human' ||
             (part as any).name === 'connect_to_support_human' ||
             part.type === 'tool-connect_to_support_human')
          ) {
            // Extract tool call ID - try multiple possible ID fields
            const toolCallId = part.toolCallId || (part as any).id || (part as any).toolUseId ||
                              `${message.id}-${(part as any).toolName}-${JSON.stringify((part as any).args || part.input)}`;

            // Only process if we haven't seen this specific tool call before
            if (!processedToolCallsRef.current.has(toolCallId)) {
              processedToolCallsRef.current.add(toolCallId);
              onConnectToHuman();
              return;
            }
          }
        }
      }
    }
  }, [messages, onConnectToHuman]);

  return (
    <div className="chat-container">
      <div className="chat-header">
        <h1>ShopNow AI</h1>
      </div>

      <div className="messages-container">
        {messages.length === 0 && (
          <div className="welcome-message">
            <h2>Welcome to ShopNow AI!</h2>
            <p>I'm your personal shopping assistant. How can I help you today?</p>
            <div className="starter-prompts">
              <button
                onClick={() => send({ role: 'user', parts: [{ type: 'text', text: 'Where is my order?' }] })}
                className="starter-button"
              >
                Where is my order?
              </button>
              <button
                onClick={() => send({ role: 'user', parts: [{ type: 'text', text: 'How do I return an item?' }] })}
                className="starter-button"
              >
                How do I return an item?
              </button>
              <button
                onClick={() => send({ role: 'user', parts: [{ type: 'text', text: 'My package was damaged' }] })}
                className="starter-button"
              >
                My package was damaged
              </button>
              <button
                onClick={() => send({ role: 'user', parts: [{ type: 'text', text: 'What are your shipping options?' }] })}
                className="starter-button"
              >
                What are your shipping options?
              </button>
            </div>
          </div>
        )}

        {messages.map((message, index) => {
          const consolidatedParts = consolidateParts(message.parts);

          return (
            <div
              key={message.id || index}
              className={`message ${message.role === 'user' ? 'user-message' : 'assistant-message'}`}
            >
              <div className="message-role">
                {message.role === 'user' ? '👤 You' : '🛍️ ShopNow AI'}
              </div>
              <div className="message-content-row">
                <div className="message-content">
                  {consolidatedParts.map((part, partIndex) => (
                    <MessagePart key={partIndex} part={part} partIndex={partIndex} />
                  ))}
                </div>
                {message.role === 'assistant' && (responseDurations.has(index) || cacheableFlags.has(index) || ttftDurations.has(index) || memoryMetadata.has(index) || memoryStorageMetadata.has(index)) && (
                  <div className="response-meta">
                    {memoryMetadata.has(index) && memoryMetadata.get(index)!.total > 0 ? (
                      <span className="memory-badge">
                        🧠 {memoryMetadata.get(index)!.total} memories retrieved
                        {memoryMetadata.get(index)!.short_term > 0 && memoryMetadata.get(index)!.long_term > 0 && (
                          <> (ST: {memoryMetadata.get(index)!.short_term}, LT: {memoryMetadata.get(index)!.long_term})</>
                        )}
                        {memoryMetadata.get(index)!.short_term > 0 && memoryMetadata.get(index)!.long_term === 0 && (
                          <> (ST: {memoryMetadata.get(index)!.short_term})</>
                        )}
                        {memoryMetadata.get(index)!.short_term === 0 && memoryMetadata.get(index)!.long_term > 0 && (
                          <> (LT: {memoryMetadata.get(index)!.long_term})</>
                        )}
                      </span>
                    ) : null}
                    {memoryStorageMetadata.has(index) && (
                      <span className="memory-badge stored">
                        💾 {memoryStorageMetadata.get(index)!.short_term + memoryStorageMetadata.get(index)!.long_term} saved
                        {memoryStorageMetadata.get(index)!.short_term > 0 && memoryStorageMetadata.get(index)!.long_term > 0 && (
                          <> (ST: {memoryStorageMetadata.get(index)!.short_term}, LT: {memoryStorageMetadata.get(index)!.long_term})</>
                        )}
                        {memoryStorageMetadata.get(index)!.short_term > 0 && memoryStorageMetadata.get(index)!.long_term === 0 && (
                          <> (ST: {memoryStorageMetadata.get(index)!.short_term})</>
                        )}
                        {memoryStorageMetadata.get(index)!.short_term === 0 && memoryStorageMetadata.get(index)!.long_term > 0 && (
                          <> (LT: {memoryStorageMetadata.get(index)!.long_term})</>
                        )}
                      </span>
                    )}
                    {(ttftDurations.has(index) || responseDurations.has(index)) && (
                      <span className="response-duration">
                        {ttftDurations.has(index) && (
                          <>TTFT {(ttftDurations.get(index)! / 1000).toFixed(1)}s</>
                        )}
                        {ttftDurations.has(index) && responseDurations.has(index) && responseDurations.get(index)! > 0 && ' · '}
                        {responseDurations.has(index) && responseDurations.get(index)! > 0 && (
                          <>Total {(responseDurations.get(index)! / 1000).toFixed(1)}s</>
                        )}
                      </span>
                    )}
                    {cacheMode !== 'off' && cacheableFlags.has(index) && (
                      <span className={`cache-badge ${cacheableFlags.get(index)!.cacheable ? 'cacheable' : 'not-cacheable'}`}>
                        {(cacheableFlags.get(index) as any)?.cache_hit
                          ? '⚡ full cache hit'
                          : (cacheableFlags.get(index) as any)?.kb_cache_hit
                            ? '🟡 kb cache hit'
                            : cacheableFlags.get(index)!.cacheable
                              ? '🟢 Response Cached'
                              : '⬜ Response not Cacheable'}
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {error && (
          <div className="error-message">
            <strong>Error:</strong> {error.message}
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <form onSubmit={(e: FormEvent) => {
        e.preventDefault();
        if (input.trim() && !isLoading) {
          send({ role: 'user', parts: [{ type: 'text', text: input }] });
          setInput('');
        }
      }} className="input-form">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about your order, returns, shipping..."
          className="message-input"
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          className="send-button"
        >
          {isLoading ? 'Sending...' : 'Send'}
        </button>
      </form>
    </div>
  );
});

export default Chat;
