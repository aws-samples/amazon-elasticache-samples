import { useState, useEffect, useRef, FormEvent } from 'react';
import './HumanChatPanel.css';

export type HumanChatStatus = 'connecting' | 'connected' | 'ended';

interface HumanChatPanelProps {
  status: HumanChatStatus | null;
  onEndChat: () => void;
  onNewHumanMessage?: () => void;
}

export default function HumanChatPanel({ status, onEndChat, onNewHumanMessage }: HumanChatPanelProps) {
  const [messages, setMessages] = useState<{id: string; role: 'user'|'agent'; text: string; timestamp: Date}[]>([]);
  const [input, setInput] = useState('');
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [showLeaveQueueDialog, setShowLeaveQueueDialog] = useState(false);
  const [elapsedTime, setElapsedTime] = useState(0);
  const [isTyping, setIsTyping] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastAgentMessageCountRef = useRef<number>(0);
  const hasGreetedRef = useRef<boolean>(false);
  const totalConnectionSeconds = 5;

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Update elapsed time during connecting state
  useEffect(() => {
    if (status === 'connecting') {
      const interval = setInterval(() => {
        setElapsedTime((prev) => prev + 1);
      }, 1000);

      return () => clearInterval(interval);
    } else {
      setElapsedTime(0);
    }
  }, [status]);

  // Detect new agent messages and notify parent
  useEffect(() => {
    const agentMessages = messages.filter(m => m.role === 'agent');
    const currentAgentMessageCount = agentMessages.length;

    // If we have more agent messages than before, notify parent
    if (currentAgentMessageCount > lastAgentMessageCountRef.current && onNewHumanMessage) {
      onNewHumanMessage();
    }

    lastAgentMessageCountRef.current = currentAgentMessageCount;
  }, [messages, onNewHumanMessage]);

  // Send initial greeting when human connects
  useEffect(() => {
    if (status === 'connected' && !hasGreetedRef.current) {
      hasGreetedRef.current = true;

      // Add greeting message from human agent
      const greetingMessage: any = {
        id: `greeting-${Date.now()}`,
        role: 'agent',
        text: "Hi! I'm a ShopNow support agent and I'll be helping you today. Could you tell me more about your issue?",
        timestamp: new Date(),
      };
      setMessages([greetingMessage]);
    } else if (status !== 'connected') {
      // Reset greeting flag when disconnected
      hasGreetedRef.current = false;
    }
  }, [status]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim() && status === 'connected') {
      // Add user message
      const userMessage: any = {
        id: Date.now().toString(),
        role: 'user',
        text: input,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);
      setInput('');

      // Fixed 5s response delay
      const delay = 5000;

      // Show typing indicator halfway through the delay
      setTimeout(() => {
        setIsTyping(true);
      }, delay / 2);

      // Auto-respond after delay
      setTimeout(() => {
        setIsTyping(false);
        const agentMessage: any = {
          id: (Date.now() + 1).toString(),
          role: 'agent',
          text: 'For this demo, pretend this is a response from a ShopNow human support agent.',
          timestamp: new Date(),
        };
        setMessages((prev) => [...prev, agentMessage]);
      }, delay);
    }
  };

  const handleEndChatClick = () => {
    setShowConfirmDialog(true);
  };

  const confirmEndChat = () => {
    setMessages([]);
    setShowConfirmDialog(false);
    onEndChat();
  };

  const cancelEndChat = () => {
    setShowConfirmDialog(false);
  };

  const handleLeaveQueueClick = () => {
    setShowLeaveQueueDialog(true);
  };

  const confirmLeaveQueue = () => {
    setShowLeaveQueueDialog(false);
    onEndChat();
  };

  const cancelLeaveQueue = () => {
    setShowLeaveQueueDialog(false);
  };

  if (!status) {
    return null;
  }

  return (
    <div className="human-chat-panel">
      <div className="human-chat-header">
        <h2>👤 Human Support Agent</h2>
        {status === 'connecting' && (
          <button onClick={handleLeaveQueueClick} className="leave-queue-button">
            Leave Queue
          </button>
        )}
        {status === 'connected' && (
          <button onClick={handleEndChatClick} className="end-chat-button">
            End Chat
          </button>
        )}
      </div>

      <div className="human-chat-content">
        {status === 'connecting' && (
          <div className="connecting-state">
            <div className="spinner-large"></div>
            <p className="connecting-text">Connecting to human agent...</p>
            <p className="wait-time">
              {(() => {
                const remainingSeconds = Math.max(0, totalConnectionSeconds - elapsedTime);
                if (remainingSeconds >= 60) {
                  const minutes = Math.round(remainingSeconds / 60);
                  return `Estimated wait time: ~${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
                } else {
                  return `Estimated wait time: less than one minute`;
                }
              })()}
            </p>
          </div>
        )}

        {status === 'connected' && (
          <>
            <div className="human-messages-container">
              {messages.length === 0 && (
                <div className="chat-started-message">
                  <p>Connected to ShopNow support agent</p>
                  <p className="helper-text">You can now chat with a live support agent</p>
                </div>
              )}

              {messages.map((message) => (
                <div
                  key={message.id}
                  className={`human-message ${message.role === 'user' ? 'human-user-message' : 'human-agent-message'}`}
                >
                  <div className="human-message-role">
                    {message.role === 'user' ? '👤 You' : '👨‍💼 Agent'}
                  </div>
                  <div className="human-message-text">{message.text}</div>
                  <div className="human-message-time">
                    {message.timestamp.toLocaleTimeString()}
                  </div>
                </div>
              ))}

              {isTyping && (
                <div className="human-message human-agent-message">
                  <div className="human-message-role">👨‍💼 Agent</div>
                  <div className="human-message-text">
                    <div className="typing-indicator">
                      <span></span>
                      <span></span>
                      <span></span>
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            <form onSubmit={handleSubmit} className="human-input-form">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Type your message..."
                className="human-message-input"
              />
              <button
                type="submit"
                disabled={!input.trim()}
                className="human-send-button"
              >
                Send
              </button>
            </form>
          </>
        )}
      </div>

      {showConfirmDialog && (
        <div className="confirm-dialog-overlay">
          <div className="confirm-dialog">
            <h3>End Chat?</h3>
            <p>Are you sure you want to end this chat with the human support agent?</p>
            <div className="confirm-dialog-buttons">
              <button onClick={cancelEndChat} className="cancel-button">
                Cancel
              </button>
              <button onClick={confirmEndChat} className="confirm-button">
                End Chat
              </button>
            </div>
          </div>
        </div>
      )}

      {showLeaveQueueDialog && (
        <div className="confirm-dialog-overlay">
          <div className="confirm-dialog">
            <h3>Leave Queue?</h3>
            <p>Are you sure you want to leave the queue? You will lose your place in line.</p>
            <div className="confirm-dialog-buttons">
              <button onClick={cancelLeaveQueue} className="cancel-button">
                Cancel
              </button>
              <button onClick={confirmLeaveQueue} className="confirm-button">
                Leave Queue
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
