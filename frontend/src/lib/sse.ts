/**
 * Server-sent events read from a POST response, since EventSource can only GET and the question stays out of URLs.
 * Yields each event's name and its JSON data. Bytes are decoded as a stream, so a character split across two chunks
 * arrives whole, and a CR that ends one chunk is held until the next, so a CRLF split across them is one line break.
 */
export async function* readEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<[string, unknown]> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let held = "";
  const take = (text: string) => {
    let joined = held + text;
    held = "";
    if (joined.endsWith("\r")) {
      held = "\r";
      joined = joined.slice(0, -1);
    }
    buffer += joined.replace(/\r\n?/g, "\n");
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    take(decoder.decode(value, { stream: true }));
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const event = parse(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      if (event) yield event;
    }
  }
  take(decoder.decode());
  // The stream is over, so whatever is left is read as it stands, a lone CR included.
  for (const block of (buffer + held.replace("\r", "\n")).split("\n\n")) {
    const event = parse(block);
    if (event) yield event;
  }
}

function parse(block: string): [string, unknown] | null {
  let type = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") type = text;
    else if (field === "data") data.push(text);
  }
  return data.length ? [type, JSON.parse(data.join("\n")) as unknown] : null;
}
