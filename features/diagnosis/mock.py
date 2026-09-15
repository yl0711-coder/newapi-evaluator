import asyncio
import json


class LocalMock:
    """An owned loopback HTTP fixture; scenario configuration is never sent to live targets."""

    def __init__(self, scenario="healthy"):
        self.scenario = scenario
        self.server = None
        self.connections = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=1_048_576)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(head) > 16_384:
                return
            lines = head.decode("ascii").split("\r\n")
            route = lines[0].split()[1]
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            length = int(next((v.strip() for k, v in headers.items() if k.lower() == "content-length"), "0"))
            if not 0 < length <= 1_048_576:
                return
            data = json.loads(await asyncio.wait_for(reader.readexactly(length), 5))
            protocol = "responses" if route.endswith("/responses") else "anthropic" if route.endswith("/messages") else "openai"
            text = data.get("input") if protocol == "responses" else data["messages"][0]["content"]
            input_tokens = len(text) // 4
            output_tokens = min(data.get("max_output_tokens", data.get("max_completion_tokens", data.get("max_tokens", 16))), 64)
            if self.scenario == "large_input_error" and input_tokens >= 1024:
                writer.write(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                await writer.drain()
                return
            usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
            if self.scenario == "missing_usage":
                usage = {}
            output = "Synthetic observation. " * 4
            if not data.get("stream"):
                if protocol == "openai":
                    body = {"choices": [{"message": {"content": output}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens} if usage else {}}
                elif protocol == "responses":
                    body = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": output}]}], "usage": usage}
                else:
                    body = {"content": [{"type": "text", "text": output}], "stop_reason": "end_turn", "usage": usage}
                encoded = json.dumps(body).encode()
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(.3 if self.scenario == "slow_first" else .01)
            if protocol == "openai":
                events = [{"choices": [{"delta": {"content": output}, "finish_reason": None}]}]
                terminal = [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
                if usage:
                    terminal.append({"choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}})
                terminal.append("[DONE]")
            elif protocol == "responses":
                events = [{"type": "response.output_text.delta", "delta": output}]
                terminal = [{"type": "response.completed", "response": {"status": "completed", "usage": usage}}]
            else:
                events = [{"type": "message_start", "message": {"usage": {"input_tokens": input_tokens} if usage else {}}},
                          {"type": "content_block_delta", "delta": {"type": "text_delta", "text": output}}]
                terminal = [{"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                             "usage": {"output_tokens": output_tokens} if usage else {}}, {"type": "message_stop"}]
            for event in events + ([] if self.scenario == "stream_break" else terminal):
                value = event if isinstance(event, str) else json.dumps(event)
                writer.write(("data: " + value + "\n\n").encode())
                await writer.drain()
                await asyncio.sleep(.01)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError, KeyError, IndexError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(task)
