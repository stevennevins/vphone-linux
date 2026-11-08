#!/usr/bin/env python3
# URB time limits in tools/vusb-usbip.py:
#   - a bulk IN URB waits for the guest with no time limit (usbmuxd drops the device when one fails);
#   - a bulk OUT URB the guest never acknowledges fails with -ETIMEDOUT (not -EIO).
#   python3 -I setup/test/vusb-usbip-timeout.py tools/vusb-usbip.py
import asyncio, collections, errno, importlib.util, os, struct, sys
os.environ["VUSB_URB_TIMEOUT"] = "0.05"
spec = importlib.util.spec_from_file_location("vusb", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.log = lambda *a: None
assert m.URB_TIMEOUT == 0.05

class ChardevWriter:
    """The guest side of the chardev: it takes requests and never answers."""
    def write(s, d): pass
    async def drain(s): pass

class W:
    def __init__(s): s.buf = b""
    def write(s, d): s.buf += d
    async def drain(s): pass

async def main():
    usb = m.VUSB(asyncio.StreamReader(), ChardevWriter())

    # Bulk IN: still waiting after many URB_TIMEOUTs; the guest's data then completes it.
    t = asyncio.create_task(usb.transfer(1, True, max_length=64))
    await asyncio.sleep(10 * m.URB_TIMEOUT)
    assert not t.done(), t
    await usb.in_data[0x81].put((m.CODE_DATA, b"late"))
    assert await asyncio.wait_for(t, 1) == b"late"

    # Bulk OUT with no acknowledgement: the bridge answers -ETIMEDOUT.
    b = m.USBIPBridge.__new__(m.USBIPBridge)
    b.write_lock = asyncio.Lock(); b.usb = usb; b.pci_stage = True; b.endpoint_sizes = {}
    b.pending = {9: (None, 2, 0)}
    w = W()
    fields = (m.CMD_SUBMIT, 9, 0x10002, 0, 2, 0, 4, 0, 0, 0, b"\0" * 8)
    await b._handle_submit(fields, b"data", w)
    cmd, seq, _, _, _, status = struct.unpack("!IIIIIi", w.buf[:24])
    assert (cmd, seq, status) == (m.RET_SUBMIT, 9, -errno.ETIMEDOUT), (cmd, seq, status)
    print("PASS")
asyncio.run(main())
