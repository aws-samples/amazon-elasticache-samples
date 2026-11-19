// A server-sent events reader for a fetch body. Pure: bytes in, events out.
// EventSource cannot POST, so the chat reads the stream itself.

export interface SseMessage {
  event: string;
  data: string;
}

/** Split complete SSE frames off `buffer`. Returns the frames and what is left over. */
export function splitFrames(buffer: string): { frames: SseMessage[]; rest: string } {
  const frames: SseMessage[] = [];
  const normalised = buffer.replace(/\r\n?/g, "\n");
  const parts = normalised.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    let event = "message";
    const data: string[] = [];
    for (const line of part.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const i = line.indexOf(":");
      const field = i === -1 ? line : line.slice(0, i);
      const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length) frames.push({ event, data: data.join("\n") });
  }
  return { frames, rest };
}

/** Read a stream of bytes as SSE messages, in order, until it ends or is aborted. */
export async function* readSse(body: ReadableStream<Uint8Array>, signal?: AbortSignal):
  AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = splitFrames(buffer);
      buffer = rest;
      for (const f of frames) yield f;
    }
    buffer += decoder.decode();
    const { frames } = splitFrames(`${buffer}\n\n`);
    for (const f of frames) yield f;
  } finally {
    reader.releaseLock();
  }
}
