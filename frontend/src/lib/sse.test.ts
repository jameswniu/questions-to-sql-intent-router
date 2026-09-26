import { describe, expect, it } from "vitest";

import { readEvents } from "@/lib/sse";

function streamOfChunks(chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      controller.close();
    },
  });
}

async function all(stream: ReadableStream<Uint8Array>) {
  const events: [string, unknown][] = [];
  for await (const event of readEvents(stream)) events.push(event);
  return events;
}

describe("readEvents", () => {
  it("reads each event's name and JSON data, in order", async () => {
    const stream = streamOfChunks([
      'event: stage\ndata: {"name":"route","ms":4}\n\n',
      ': a comment line is skipped\n\nevent: done\ndata: {"request_id":"r1"}\n\n',
    ]);
    expect(await all(stream)).toEqual([
      ["stage", { name: "route", ms: 4 }],
      ["done", { request_id: "r1" }],
    ]);
  });

  it("keeps a CRLF split across two chunks as one line break", async () => {
    const stream = streamOfChunks(["event: stage\r", '\ndata: {"name":"sql"}\r\n\r', "\n"]);
    expect(await all(stream)).toEqual([["stage", { name: "sql" }]]);
  });

  it("reads a character whose bytes arrive in two chunks", async () => {
    const bytes = new TextEncoder().encode('event: answer\ndata: {"text":"Colorado · Q2"}\n\n');
    const cut = bytes.indexOf(0xc2) + 1;
    expect(await all(streamOfChunks([bytes.slice(0, cut), bytes.slice(cut)]))).toEqual([
      ["answer", { text: "Colorado · Q2" }],
    ]);
  });

  it("reads a last event the stream ends without its blank line", async () => {
    expect(await all(streamOfChunks(['event: done\ndata: {"outcome":"answer"}']))).toEqual([
      ["done", { outcome: "answer" }],
    ]);
  });
});
