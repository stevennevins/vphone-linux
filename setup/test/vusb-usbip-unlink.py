#!/usr/bin/env python3
# A USB/IP CMD_UNLINK and its RET_UNLINK are 48 bytes each (struct usbip_header).
# Feeds two unlinks to tools/vusb-usbip.py and checks it reads and answers each whole.
#   python3 -I setup/test/vusb-usbip-unlink.py tools/vusb-usbip.py
import asyncio, importlib.util, struct, sys
spec = importlib.util.spec_from_file_location("vusb", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
logs = []; m.log = lambda *a: logs.append(" ".join(map(str, a)))
class W:
    def __init__(s): s.buf = b""
    def write(s, d): s.buf += d
    async def drain(s): pass
    def close(s): pass
    async def wait_closed(s): pass
class FakeUSB: pass
async def main():
    b = m.USBIPBridge.__new__(m.USBIPBridge)
    b.write_lock = asyncio.Lock(); b.clients = set(); b.pending = {}
    async def imp(r, w): return True
    b.handle_import = imp
    r = asyncio.StreamReader()
    r.feed_data(struct.pack("!HHI", m.USBIP_VERSION, m.OP_REQ_IMPORT, 0))
    unlink = struct.pack("!IIIIII24x", m.CMD_UNLINK, 7, 0x10002, 0, 1, 5)
    assert len(unlink) == 48
    r.feed_data(unlink * 2); r.feed_eof()
    w = W()
    await b.handle_connection(r, w)
    print("logs:", logs)
    print("reply bytes:", len(w.buf))
    assert not logs, logs
    assert len(w.buf) == 96
    assert struct.unpack("!IIIIIi", w.buf[:24]) [0] == m.RET_UNLINK
    print("PASS")
asyncio.run(main())
