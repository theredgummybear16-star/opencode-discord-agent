import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import sys
import termios

from aiohttp import web, WSMsgType

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7681
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden}
#term{height:100vh;width:100vw;padding:4px;box-sizing:border-box}
</style></head>
<body><div id="term"></div>
<script>
const t=new Terminal({cursorBlink:true,fontSize:14,scrollback:5000,
  theme:{background:'#000',foreground:'#e6e6e6',cursor:'#ffffff'}});
t.open(document.getElementById('term'));
const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws?k=__TOKEN__');
ws.onopen=()=>{t.focus();ws.send(JSON.stringify({cols:t.cols,rows:t.rows}))};
ws.onmessage=e=>t.write(typeof e.data==='string'?e.data:new TextDecoder().decode(e.data));
ws.onclose=()=>t.write('\\r\\n[connection closed]\\r\\n');
ws.onerror=()=>t.write('\\r\\n[connection error]\\r\\n');
t.onData(d=>{if(ws.readyState===WebSocket.OPEN)ws.send(d)});
t.onResize(s=>{if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({cols:s.cols,rows:s.rows}))});
</script></body></html>
"""


def set_size(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _ok(request):
    return request.query.get("k") == TOKEN


async def index(request):
    if not _ok(request):
        return web.Response(status=403, text="forbidden")
    return web.Response(content_type="text/html", text=HTML.replace("__TOKEN__", TOKEN))


async def _send_text(ws, closed, text):
    try:
        if not ws.closed:
            await ws.send_str(text)
    except Exception:
        closed.set()


async def ws_handler(request):
    if not _ok(request):
        return web.Response(status=403, text="forbidden")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    pid, master = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["HOME"] = os.environ.get("HOME", "/home/runner")
        try:
            os.execvp("/bin/bash", ["/bin/bash", "-l"])
        except Exception:
            os._exit(1)
    loop = asyncio.get_event_loop()
    closed = asyncio.Event()

    def pump():
        try:
            data = os.read(master, 65536)
        except OSError:
            closed.set()
            loop.remove_reader(master)
            return
        if not data:
            closed.set()
            loop.remove_reader(master)
            return
        if not ws.closed:
            try:
                asyncio.ensure_future(_send_text(ws, closed, data.decode("utf-8", "replace")))
            except Exception:
                closed.set()

    set_size(master, 100, 30)
    loop.add_reader(master, pump)

    async def recv():
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
            if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                data = msg.data
                if isinstance(data, str):
                    if data.startswith("{") and '"cols"' in data:
                        try:
                            j = json.loads(data)
                            set_size(master, int(j["cols"]), int(j["rows"]))
                            continue
                        except Exception:
                            pass
                    data = data.encode()
                if ws.closed:
                    break
                try:
                    os.write(master, data)
                except OSError:
                    break
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                break

    recv_task = asyncio.ensure_future(recv())
    await recv_task
    try:
        loop.remove_reader(master)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(master)
    except Exception:
        pass
    if not ws.closed:
        await ws.close()
    return ws


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()