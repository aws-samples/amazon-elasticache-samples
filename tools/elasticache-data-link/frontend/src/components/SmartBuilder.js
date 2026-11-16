import React, { useState, useRef, useEffect } from 'react';
import axios from 'axios';
import './Servers.css';
import API_URL from '../apiUrl';

const CODE_STYLE = {
  background: '#f0f0f0', padding: '1px 5px', borderRadius: '3px', fontSize: '12px',
};

// `**bold**` and `inline code` as React elements. Everything else stays plain
// text, which React escapes, so a message containing <img onerror=...> renders
// as those characters instead of running.
export function formatInline(line) {
  const parts = [];
  const pattern = /\*\*(.+?)\*\*|`(.+?)`/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(line)) !== null) {
    if (match.index > last) parts.push(line.slice(last, match.index));
    if (match[1] !== undefined) {
      parts.push(<strong key={match.index}>{match[1]}</strong>);
    } else {
      parts.push(<code key={match.index} style={CODE_STYLE}>{match[2]}</code>);
    }
    last = pattern.lastIndex;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}

function SmartBuilder() {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: "Hi! I'm the ECDL Smart Pipeline Builder. Tell me what data you want to replicate and where, and I will help you set up the pipeline.\n\nFor example:\n- \"Replicate the orders and customers tables from our Postgres to ElastiCache\"\n- \"Set up a CDC pipeline from our production database to Valkey\"\n- \"What sources and brokers do I already have configured?\"\n\nWhat would you like to build?" }
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    if (!loading) inputRef.current?.focus();
  }, [loading]);

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg = { role: 'user', content: text };
    const updatedMessages = [...messages, userMsg];
    setMessages(updatedMessages);
    setInput('');
    setLoading(true);

    try {
      // Send full conversation history (excluding the initial greeting)
      const chatHistory = updatedMessages.filter((m, i) => i > 0 || m.role === 'user');
      const res = await axios.post(`${API_URL}/api/smart-builder/chat`, {
        messages: chatHistory
      });
      setMessages(prev => [...prev, { role: 'assistant', content: res.data.response }]);
    } catch (e) {
      const errMsg = e.response?.data?.error || e.message;
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `⚠️ Error: ${errMsg}\n\nThis might be a Bedrock connectivity issue. Make sure the backend has access to Amazon Bedrock in your region.`
      }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const formatMessage = (text) => {
    // Simple markdown-like formatting, built as React elements rather than an
    // HTML string: model output and error messages are untrusted, and inserting
    // them as HTML would run any markup they contain in the operator's browser.
    return text.split('\n').map((line, i) => {
      // Code blocks
      if (line.startsWith('```')) return null;
      // Bullet points
      if (line.startsWith('- ')) {
        return <div key={i} style={{ paddingLeft: '15px', position: 'relative' }}>
          <span style={{ position: 'absolute', left: '0' }}>•</span>
          <span>{formatInline(line.substring(2))}</span>
        </div>;
      }
      if (line.trim() === '') return <br key={i} />;
      return <div key={i}>{formatInline(line)}</div>;
    });
  };

  return (
    <div className="servers-container" style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 40px)' }}>
      <div className="header">
        <h1>🤖 Smart Pipeline Builder</h1>
        <button className="btn btn-secondary" onClick={() => setMessages([messages[0]])}>
          New Conversation
        </button>
      </div>

      {/* Chat messages */}
      <div style={{
        flex: 1, overflowY: 'auto', padding: '20px',
        display: 'flex', flexDirection: 'column', gap: '16px'
      }}>
        {messages.map((msg, i) => (
          <div key={i} style={{
            display: 'flex',
            justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start'
          }}>
            <div style={{
              maxWidth: '75%', padding: '12px 16px', borderRadius: '12px',
              fontSize: '14px', lineHeight: '1.6',
              background: msg.role === 'user' ? '#3498db' : '#f0f2f5',
              color: msg.role === 'user' ? 'white' : '#2c3e50',
              borderBottomRightRadius: msg.role === 'user' ? '4px' : '12px',
              borderBottomLeftRadius: msg.role === 'user' ? '12px' : '4px',
              boxShadow: '0 1px 2px rgba(0,0,0,0.1)'
            }}>
              {msg.role === 'assistant' && (
                <div style={{ fontSize: '11px', color: '#888', marginBottom: '6px', fontWeight: 600 }}>
                  AI Agent
                </div>
              )}
              {formatMessage(msg.content)}
            </div>
          </div>
        ))}
        {loading && (
          <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
            <div style={{
              padding: '12px 16px', borderRadius: '12px', borderBottomLeftRadius: '4px',
              background: '#f0f2f5', boxShadow: '0 1px 2px rgba(0,0,0,0.1)'
            }}>
              <div style={{ fontSize: '11px', color: '#888', marginBottom: '6px', fontWeight: 600 }}>AI Agent</div>
              <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
                <span style={{ animation: 'pulse 1.4s infinite', animationDelay: '0s' }}>●</span>
                <span style={{ animation: 'pulse 1.4s infinite', animationDelay: '0.2s' }}>●</span>
                <span style={{ animation: 'pulse 1.4s infinite', animationDelay: '0.4s' }}>●</span>
                <span style={{ marginLeft: '8px', fontSize: '13px', color: '#888' }}>Thinking...</span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div style={{
        padding: '15px 20px', borderTop: '1px solid #e0e0e0', background: 'white',
        display: 'flex', gap: '10px', alignItems: 'flex-end'
      }}>
        <textarea
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Describe what you want to build..."
          disabled={loading}
          rows={1}
          style={{
            flex: 1, padding: '10px 14px', border: '1px solid #ddd', borderRadius: '8px',
            fontSize: '14px', resize: 'none', fontFamily: 'inherit',
            minHeight: '42px', maxHeight: '120px', outline: 'none',
            transition: 'border-color 0.2s'
          }}
          onFocus={e => e.target.style.borderColor = '#3498db'}
          onBlur={e => e.target.style.borderColor = '#ddd'}
        />
        <button className="btn btn-primary" onClick={sendMessage}
          disabled={loading || !input.trim()}
          style={{ height: '42px', minWidth: '80px' }}>
          {loading ? '...' : 'Send'}
        </button>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 80%, 100% { opacity: 0.3; }
          40% { opacity: 1; }
        }
      `}</style>
    </div>
  );
}

export default SmartBuilder;
