#!/usr/bin/env python3
# CMD_UNLINK handling in tools/vusb-usbip.py:
#   - each CMD_UNLINK and RET_UNLINK is 48 bytes (struct usbip_header);
#   - after a RET_UNLINK with status 0, no RET_SUBMIT for that URB follows (vhci-hcd drops the
#     connection on a RET_SUBMIT for a seqnum it gave back);
#   - a URB that already has its RET_SUBMIT gets RET_UNLINK -ENOENT, after the RET_SUBMIT;
#   - unlinking a URB queued behind another does not abort the other's transfer.
#   python3 -I setup/test/vusb-usbip-unlink.py tools/vusb-usbip.py
import asyncio, collections, errno, importlib.util, struct, sys
spec = importlib.util.spec_from_file_location("vusb", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
logs = []; m.log = lambda *a: logs.append(" ".join(map(str, a)))

class W:
    def __init__(s): s.buf = b""
    def write(s, d): s.buf += d
    async def drain(s): await asyncio.sleep(0)
    def close(s): pass
    async def wait_closed(s): pass

class FakeUSB:
    """Bulk IN on ep 1. A transfer waits for release() or fails when aborted, as QEMU does."""
    def __init__(s): s.waiting = collections.deque(); s.aborts = 0
    async def transfer(s, ep, direction_in, payload=b"", max_length=None):
        f = asyncio.get_running_loop().create_future(); s.waiting.append(f)
        return await f
    def release(s, data):
        s.waiting.popleft().set_result(data)
    async def abort(s, ep, direction_in):
        s.aborts += 1
        if s.waiting:
            s.waiting.popleft().set_exception(OSError(errno.EIO, "aborted"))
        await asyncio.sleep(0)

def bridge():
    b = m.USBIPBridge.__new__(m.USBIPBridge)
    b.write_lock = asyncio.Lock(); b.clients = set(); b.pending = {}; b.active = {}
    b.urb_locks = collections.defaultdict(asyncio.Lock); b.pci_stage = True; b.endpoint_sizes = {}
    b.usb = FakeUSB()
    async def imp(r, w): return True
    b.handle_import = imp
    return b

def submit(seq): return struct.pack("!IIIIIIiiii8s", m.CMD_SUBMIT, seq, 0x10002, 1, 1, 0, 64, 0, 0, 0, b"\0" * 8)
def unlink(seq, target): return struct.pack("!IIIIII24x", m.CMD_UNLINK, seq, 0x10002, 0, 1, target)

def replies(buf):
    """(command, seqnum, status) for each reply; RET_SUBMIT carries actual_length bytes of data."""
    out = []
    while buf:
        cmd, seq, _, _, _, status, actual = struct.unpack("!IIIIIii", buf[:28])
        out.append((cmd, seq, status)); buf = buf[48 + (actual if cmd == m.RET_SUBMIT else 0):]
    return out

async def settle():
    for _ in range(20): await asyncio.sleep(0)

async def run(steps):
    b = bridge(); r = asyncio.StreamReader(); w = W()
    r.feed_data(struct.pack("!HHI", m.USBIP_VERSION, m.OP_REQ_IMPORT, 0))
    conn = asyncio.create_task(b.handle_connection(r, w))
    for step in steps:
        step(b, r) if callable(step) else r.feed_data(step)
        await settle()
    r.feed_eof(); await conn
    return b, replies(w.buf)

async def main():
    # Two unlinks of unknown URBs: two whole 48-byte replies, nothing logged.
    b, got = await run([unlink(7, 5) * 2])
    assert got == [(m.RET_UNLINK, 7, -errno.ENOENT)] * 2, got

    # The unlink's abort makes the transfer fail: only the RET_UNLINK goes out.
    b, got = await run([submit(10), unlink(11, 10)])
    assert got == [(m.RET_UNLINK, 11, 0)], got

    # The URB completes first: RET_SUBMIT, then RET_UNLINK -ENOENT.
    b, got = await run([submit(20), lambda b, r: b.usb.release(b"x" * 16), unlink(21, 20)])
    assert got == [(m.RET_SUBMIT, 20, 0), (m.RET_UNLINK, 21, -errno.ENOENT)], got

    # Unlink a URB queued behind another: no abort; the running one still completes.
    b, got = await run([submit(30) + submit(31), unlink(32, 31), lambda b, r: b.usb.release(b"y" * 8)])
    assert b.usb.aborts == 0, b.usb.aborts
    assert got == [(m.RET_UNLINK, 32, 0), (m.RET_SUBMIT, 30, 0)], got

    assert not logs, logs
    print("PASS")
asyncio.run(main())
