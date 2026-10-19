import { afterEach, describe, expect, it, vi } from 'vitest';
import { EventSourceTransport, SseClient, type StreamTransport } from '../src/stream/sse-client.js';
import { uuid } from './helpers.js';

type Handlers = Parameters<StreamTransport['open']>[2];

function harness() {
  const connections: { url: string; handlers: Handlers; close: ReturnType<typeof vi.fn> }[] = [];
  const events: number[] = [];
  const client = new SseClient({
    baseUrl: 'http://gateway.test',
    onEvent: (event) => events.push(event.seq),
    onStatus: () => {},
    transport: { open(url, _cursor, handlers) {
      const connection = { url, handlers, close: vi.fn() };
      connections.push(connection);
      return connection.close;
    } },
  });
  client.connect(uuid(50));
  function frame(connection: number, seq: number, workflowId = uuid(50)) {
    connections[connection]!.handlers.onEvent(undefined, JSON.stringify({
      event_id: seq.toString(16).padStart(32, '0'), workflow_id: workflowId,
      seq, type: 'notice', payload: { text: `${seq}` },
      created_at: '2026-10-18T00:00:00Z', synthetic: true,
    }));
  }
  return { client, connections, events, frame };
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('SSE replay preserves a contiguous cursor', () => {
  it('recovers the missing events before applying a later frame', () => {
    const { client, connections, events, frame } = harness();
    frame(0, 0);
    frame(0, 2);
    expect(events).toEqual([0]);
    expect(client.lastSeq).toBe(0);
    expect(new URL(connections[1]!.url).searchParams.get('last_event_id')).toBe('0'.repeat(32));
    frame(1, 1);
    frame(1, 2);
    expect(events).toEqual([0, 1, 2]);
    client.disconnect();
  });

  it('does not rewind the cursor when the server repeats an older event', () => {
    const { client, connections, frame } = harness();
    frame(0, 0); frame(0, 1); frame(0, 0);
    client.replay();
    expect(new URL(connections[1]!.url).searchParams.get('last_event_id')).toBe('1'.padStart(32, '0'));
    client.disconnect();
  });

  it('ignores callbacks from a closed connection and from another workflow', () => {
    const { client, events, frame } = harness();
    client.replay();
    frame(0, 0);
    frame(1, 0, uuid(51));
    expect(events).toEqual([]);
    frame(1, 0);
    expect(events).toEqual([0]);
    client.disconnect();
  });

  it('manual replay cancels a scheduled reconnect and closes the old transport', () => {
    vi.useFakeTimers();
    const { client, connections } = harness();
    connections[0]!.handlers.onError(true);
    expect(connections[0]!.close).toHaveBeenCalledOnce();
    client.replay();
    vi.advanceTimersByTime(10000);
    expect(connections).toHaveLength(2);
    client.disconnect();
    connections[1]!.handlers.onError(true);
    vi.advanceTimersByTime(10000);
    expect(connections).toHaveLength(2);
  });

  it('closes native EventSource before its own reconnect loop takes over', () => {
    const source = { close: vi.fn(), readyState: 0, onerror: undefined as (() => void) | undefined };
    vi.stubGlobal('EventSource', Object.assign(function () { return source; }, { CLOSED: 2 }));
    const onError = vi.fn();
    new EventSourceTransport().open('http://gateway.test/events', undefined, {
      onOpen: () => {}, onEvent: () => {}, onError,
    });
    source.onerror!();
    expect(source.close).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(true);
  });
});
