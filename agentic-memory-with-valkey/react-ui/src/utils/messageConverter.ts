/**
 * Converts Strands agent message format to ai-sdk UIMessage format.
 *
 * Strands splits a single agent "turn" into multiple messages:
 *   assistant (text + toolUse) → user (toolResult) → assistant (toolUse) → user (toolResult) → assistant (text)
 *
 * ai-sdk expects one assistant UIMessage per turn with all tool calls and text merged:
 *   { id, role: "assistant", parts: [text, tool-<name>, tool-<name>, text, ...] }
 *
 * Tool parts use type "tool-<toolName>" with input/output merged into one part.
 */

interface StrandsMessage {
  role: string;
  content: Array<Record<string, any>> | string;
}

interface UIMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  parts: Array<Record<string, any>>;
}

/**
 * Check if a message's content is exclusively toolResult blocks (no real user text).
 */
function isToolResultOnly(msg: StrandsMessage): boolean {
  const content = msg.content;
  if (typeof content === 'string') return false;
  if (!Array.isArray(content) || content.length === 0) return true;
  return content.every((block) => 'toolResult' in block);
}

export function strandsToUIMessages(strandsMessages: StrandsMessage[]): UIMessage[] {
  // First pass: build a map of toolUseId → result content from all toolResult blocks
  const toolResults = new Map<string, any>();
  for (const msg of strandsMessages) {
    const content = msg.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if ('toolResult' in block) {
        const tr = block.toolResult;
        const id = tr.toolUseId || tr.id || '';
        let resultContent: any = '';
        if (Array.isArray(tr.content)) {
          resultContent = tr.content
            .filter((c: any) => c.text)
            .map((c: any) => c.text)
            .join('\n');
        }
        if (id) toolResults.set(id, resultContent);
      }
    }
  }

  // Second pass: group messages into "turns".
  // A turn starts with a real user message (has actual text, not just toolResults)
  // and includes all subsequent assistant + toolResult-only-user messages until
  // the next real user message.
  const result: UIMessage[] = [];
  let i = 0;

  while (i < strandsMessages.length) {
    const msg = strandsMessages[i];
    const role = msg.role as 'user' | 'assistant' | 'system';

    // Skip system messages
    if (role === 'system') { i++; continue; }

    // Real user message — extract text parts
    if (role === 'user' && !isToolResultOnly(msg)) {
      const parts = extractUserParts(msg, i);
      if (parts.length > 0) {
        result.push({
          id: `restored_${i}_${Date.now()}`,
          role: 'user',
          parts,
        });
      }
      i++;
      continue;
    }

    // Tool-result-only user message — skip (already consumed via map)
    if (role === 'user' && isToolResultOnly(msg)) {
      i++;
      continue;
    }

    // Assistant message — collect this and all continuation assistant messages
    // (those separated only by toolResult-only user messages) into one UIMessage
    if (role === 'assistant') {
      const mergedParts: Array<Record<string, any>> = [];
      
      while (i < strandsMessages.length) {
        const cur = strandsMessages[i];
        
        if (cur.role === 'assistant') {
          // Extract parts from this assistant chunk
          const chunkParts = extractAssistantParts(cur, i, toolResults);
          mergedParts.push(...chunkParts);
          i++;
          continue;
        }
        
        // toolResult-only user message — skip it and continue merging
        if (cur.role === 'user' && isToolResultOnly(cur)) {
          i++;
          continue;
        }
        
        // Real user message or anything else — stop merging
        break;
      }

      if (mergedParts.length > 0) {
        result.push({
          id: `restored_assistant_${i}_${Date.now()}`,
          role: 'assistant',
          parts: mergedParts,
        });
      }
      continue;
    }

    // Fallback — skip unknown
    i++;
  }

  return result;
}

/**
 * Strip the "## Relevant Context from Memory" block that the backend prepends
 * to user messages when memory retrieval is active. The block format is:
 *
 *   ## Relevant Context from Memory\n\n1. [Type] content\n2. [Type] content\n\n<real user text>
 *
 * We find the marker and then skip past the last numbered-list line to get
 * to the real user message.
 */
function stripMemoryPrefix(text: string): string {
  const marker = '## Relevant Context from Memory';
  const idx = text.indexOf(marker);
  if (idx === -1) return text;

  // Find the end of the numbered memory lines.
  // The memory block ends where the numbered list stops — look for the last
  // line matching /^\d+\. \[/ and take everything after it.
  const afterMarker = text.substring(idx);
  const lines = afterMarker.split('\n');
  let lastMemoryLineIdx = 0;
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    // Header line or numbered memory item or empty line within the block
    if (trimmed.startsWith('## Relevant Context') || /^\d+\.\s*\[/.test(trimmed) || trimmed === '') {
      lastMemoryLineIdx = i;
    } else {
      // First non-memory, non-empty line — this is the real user text
      break;
    }
  }

  const realText = lines.slice(lastMemoryLineIdx + 1).join('\n').trim();
  const before = text.substring(0, idx).trim();
  return before ? before + '\n' + realText : realText;
}

function extractUserParts(msg: StrandsMessage, idx: number): Array<Record<string, any>> {
  const parts: Array<Record<string, any>> = [];
  const content = msg.content;

  if (typeof content === 'string') {
    const cleaned = stripMemoryPrefix(content);
    if (cleaned) parts.push({ type: 'text', text: cleaned });
  } else if (Array.isArray(content)) {
    for (const block of content) {
      if ('text' in block && block.text) {
        let text = stripMemoryPrefix(block.text);
        const cacheSignalIdx = text.lastIndexOf('{"_shopnow_cache":');
        if (cacheSignalIdx !== -1) {
          text = text.substring(0, cacheSignalIdx).trimEnd();
        }
        if (text) {
          parts.push({ type: 'text', text });
        }
      }
    }
  }
  return parts;
}

function extractAssistantParts(
  msg: StrandsMessage,
  idx: number,
  toolResults: Map<string, any>,
): Array<Record<string, any>> {
  const parts: Array<Record<string, any>> = [];
  const content = msg.content;

  if (typeof content === 'string') {
    parts.push({ type: 'text', text: content });
    return parts;
  }

  if (!Array.isArray(content)) return parts;

  for (const block of content) {
    if ('text' in block && block.text) {
      let text = block.text;
      const cacheSignalIdx = text.lastIndexOf('{"_shopnow_cache":');
      if (cacheSignalIdx !== -1) {
        text = text.substring(0, cacheSignalIdx).trimEnd();
      }
      if (text) {
        parts.push({ type: 'text', text });
      }
    } else if ('toolUse' in block) {
      const tu = block.toolUse;
      const toolName = tu.name || 'unknown';
      const toolCallId = tu.toolUseId || tu.id || `tool_${idx}_${parts.length}`;
      parts.push({
        type: `tool-${toolName}`,
        toolCallId,
        state: 'result',
        input: tu.input || {},
        output: toolResults.get(toolCallId) || '',
      });
    }
    // Skip reasoningContent and toolResult blocks
  }

  return parts;
}
