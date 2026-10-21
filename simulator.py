"""
simulator.py - software model of the LCD, used by the self-tests and by `app.py --demo`
(lets you try the whole interface without the screen attached).
"""
import struct

import lcdproto
from lcdproto import LcdError


class MockDevice:
    """Parses CBW/CDB exactly as the byte layouts recovered from the original exe."""
    W, H = 480, 480

    def __init__(self):
        self.flash = bytearray(b"\xff" * 0x400000)
        self.settings = None
        self.offset = None
        self.off_mode = None
        self.frames = []
        self.log = []
        self.pending = None      # (tag, expect, kind)
        self.inq = []
        self.echo_tag = True
        self.busy_cbw = 0           # number of CBW writes that time out (device busy)
        self.late_csw = 0           # number of CSW reads that time out before the reply shows up
        self.strict_start = False   # model: reject everything until info was answered
        self.info_done = False
        self.last_tag, self.last_dlen = 0, 0
        self.fail_data = False

    def bulk_out(self, ep, data, timeout):
        assert ep == 0x01
        data = bytes(data)
        if self.pending is None and self.busy_cbw:
            self.busy_cbw -= 1
            raise lcdproto.LcdTimeout("bulk OUT failed: timed out")
        if self.pending is None:
            sig, tag, dlen, flags, lun, cblen = struct.unpack_from("<IIIBBB", data)
            assert sig == 0x43425355 and cblen == 0x10 and len(data) == 31 and lun == 0
            cdb = data[15:31]
            self.log.append("cd%02x" % cdb[5] if cdb[0] == 0xCD else cdb[:2].hex())
            self.cur = (tag, dlen, flags, cdb)
            is_info = cdb[0] == 0xCD and cdb[5] == 3
            if self.strict_start and not self.info_done and not is_info:
                # observed on hardware: UTRS, previous tag, previous length as residue, status 2
                self.inq.append(struct.pack("<IIIB", 0x53525455, self.last_tag, self.last_dlen, 2))
                if dlen and not (flags & 0x80):
                    self.pending = "discard"       # the host still sends its data phase; the screen ignores it
                return
            self.last_tag, self.last_dlen = tag, dlen
            if dlen and not (flags & 0x80):
                self.pending = True          # expect data phase
                return
            self._execute(b"")
        elif self.pending == "discard":
            self.pending = None
        else:
            self.pending = None
            tag, dlen, flags, cdb = self.cur
            assert len(data) == dlen, (len(data), dlen)
            if self.fail_data:
                self.fail_data = False
                self._csw(1)          # device reports failure, host must still consume this CSW
                raise LcdError("data phase timeout")
            self._execute(data)

    def _csw(self, status=0):
        tag = self.cur[0] if self.echo_tag else 0xEFBEADDE
        self.inq.append(struct.pack("<IIIB", 0x53525455, tag, 0, status))

    def _execute(self, data):
        tag, dlen, flags, c = self.cur
        if c[0] == 0xCD and c[5] == 3:
            assert flags == 0x80 and dlen == 5
            self.info_done = True
            self.inq.append(struct.pack("<HHB", self.W, self.H, 1))
        elif c[0] == 0xCD and c[5] == 4:
            self.off_mode = c[6]
            if self.strict_start:
                self.info_done = False      # model: screen leaves PC mode, needs a new info handshake
        elif c[0] == 0xCD and c[5] == 6 and c[6] == 0x30:
            x0, y0, x1, y1 = struct.unpack_from("<HHHH", c, 7)
            assert (x0, y0, x1, y1) == (0, 0, self.W - 1, self.H - 1)
            assert data[:2] == b"\xff\xd8"
            self.frames.append(data)
        elif c[0] == 0xFE and c[1] == 0xD1:
            self.offset = struct.unpack_from("<HH", c, 8)
        elif c[0] == 0xFB and c[1] == 0xD8:
            off, = struct.unpack_from(">I", c, 8)
            assert off % 0x1000 == 0 and dlen == 0
            self.flash[off:off + 0x1000] = b"\xff" * 0x1000
        elif c[0] == 0xFB and c[1] == 0xD9:
            off, n = struct.unpack_from(">II", c, 8)
            assert n == dlen == len(data)
            assert all(b == 0xFF for b in self.flash[off:off + n]), "write to unerased flash"
            self.flash[off:off + n] = data
        elif c[0] == 0xFB and c[1] == 0xDA:
            assert struct.unpack_from(">II", c, 8) == (0, 0x200) and len(data) == 0x200
            self.settings = data
        else:
            raise AssertionError("unknown CDB " + c.hex())
        self._csw()

    def bulk_in(self, ep, length, timeout):
        assert ep == 0x81
        if self.late_csw and length == 13:
            self.late_csw -= 1
            raise lcdproto.LcdTimeout("bulk IN failed: timed out")
        if not self.inq:
            raise lcdproto.LcdTimeout("bulk IN failed: timed out")
        return self.inq.pop(0)

    def drain(self):
        got, self.inq = list(self.inq), []
        return got

    def clear_halt(self, ep):
        pass

    def close(self):
        pass
