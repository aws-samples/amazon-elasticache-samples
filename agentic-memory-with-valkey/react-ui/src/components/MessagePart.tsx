import ReactMarkdown from 'react-markdown';
import './MessagePart.css';

interface MessagePartProps {
  part: any;
  partIndex: number;
}

export default function MessagePart({ part }: MessagePartProps) {
  if (part.type === 'llm-call') {
    return (
      <div className="llm-call-marker">
        LLM Call {part.index}
      </div>
    );
  }

  if (part.type === 'text') {
    // Strip the cache signal JSON that the LLM appends (server-side strips it from
    // the stored response, but it streams through to the UI in real-time)
    const cleaned = (part.text || '').replace(/\{"_shopnow_cache":\s*\{[^}]*\}\}\s*$/s, '').trimEnd();
    if (!cleaned) return null;
    return (
      <div className="message-text">
        <ReactMarkdown
          components={{
            code: ({ children, className }) => {
              // Fenced code blocks get a className like "language-js"
              if (className) {
                return <pre><code className={className}>{children}</code></pre>;
              }
              // Inline code
              return <code className="inline-code">{children}</code>;
            },
          }}
        >
          {cleaned}
        </ReactMarkdown>
      </div>
    );
  }

  if (part.type === 'reasoning') {
    const tools: string[] = part.tools || [];
    return (
      <details className="reasoning-block">
        <summary className="reasoning-summary">
          <span className="reasoning-label">{part.state === 'streaming' ? 'Thinking...' : 'Thinking'}</span>
          {tools.map((t: string, i: number) => (
            <span key={i} className="reasoning-tool-badge">{t}</span>
          ))}
        </summary>
        <div className="reasoning-content">{part.text}</div>
      </details>
    );
  }

  // Tool calls: SDK emits type "tool-<toolName>"
  if (part.type?.startsWith('tool-')) {
    const toolName = part.type.slice(5); // strip "tool-" prefix
    return (
      <div className="tool-invocation">
        <span className="tool-name">{toolName}</span>
      </div>
    );
  }

  // Ignore internal data parts
  if (part.type?.startsWith('data-')) {
    return null;
  }

  return null;
}
