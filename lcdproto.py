"""
lcdproto.py - USB protocol for the Appotech AX206 2.1" cooler LCD (VID 0x2A55 / PID 0x9990),
reconstructed by reverse engineering the vendor's Windows application.

Every command is a USB Bulk-Only-Transport frame:
    CBW (31 bytes, ep 0x01)  ->  optional data phase  ->  CSW (13 bytes, ep 0x81)
with a 16-byte vendor CDB.  Addresses in comments refer to the vendor application.

CDB summary (all 16 bytes, unlisted bytes are 0):
    CD 00 00 00 00 03                     -> device info, 5 bytes IN: W(le16) H(le16) ?   (0x420c08)
    CD 00 00 00 00 04 <v>                 -> set "Off Mode" (0=Off Lcd, 1=Play Res)       (0x41ffc9/0x42113a)
    CD 00 00 00 00 06 30 00 00 00 00 W-1(le16) H-1(le16)  + JPEG data OUT   -> blit frame (0x41e2b8)
    FE D1 00*6 X(le16) Y(le16)            -> set local-play X/Y offset                     (0x41feea)
    FB D8 00*6 off(be32)                  -> erase 4 KiB at flash offset                   (0x413eae)
    FB D9 00*6 off(be32) len(be32)        -> write len bytes at flash offset (data OUT)    (0x413f98/0x41404f)
    FB DA 00*6 00000000  00000200         -> write 512-byte settings page (data OUT)       (0x420ab7/0x4208f8)
                                             page = [WorkMode, OffMode, LcdDir, 0xFF * 509]
"""
import errno
import os
import struct
import sys
import time

VID, PID = 0x2A55, 0x9990
EP_OUT, EP_IN = 0x01, 0x81
CBW_SIG = 0x43425355          # "USBC"  (0x417644)
SECTOR = 0x1000               # write/erase granularity (0x413edf)
MAX_RES = 0x3B6000            # "Please load the picture Size <=3.7MB!!" (0x413da8)
SETTINGS_LEN = 0x200

T_CBW, T_CSW, T_DATA = 1000, 1000, 2000   # ms timeouts (0x417660, 0x417540, 0x413fa9)

# Debug switches (also settable with the --trace / --lax-csw CLI flags or LCD_TRACE / LCD_LAX_CSW env vars)
TRACE = bool(os.environ.get("LCD_TRACE"))
LAX_CSW = bool(os.environ.get("LCD_LAX_CSW"))   # accept a CSW whose tag differs (status byte is still checked)
# The original app ALWAYS starts a session with the info command and then does Sleep(1000)
# (handler 0x420b50, Sleep at 0x420cc6) before any other command, on a handle it keeps open.
HANDSHAKE = not os.environ.get("LCD_NO_HANDSHAKE")
SETTLE_S = 1.0
CBW_RETRIES = 3      # a CBW that times out was never accepted (device busy) -> safe to resend
CSW_RETRIES = 4      # slow commands (flash erase) may answer late
_T0 = time.perf_counter()
TRACE_FILE = None    # set when there is no console (windowed exe): trace lines are appended to this file


class LcdError(Exception):
    pass


class MediaError(LcdError):
    """The chosen FILE is the problem (unreadable, too big...), not the USB link."""


class LcdTimeout(LcdError):
    """A bulk transfer timed out (device NAKing: busy erasing/decoding, or not listening)."""


# --------------------------------------------------------------------------- transports
class PyUSBTransport:
    """libusb through pyusb.  On Windows the device must be bound to WinUSB/libusbK."""

    def __init__(self):
        import usb.core
        import usb.util
        self.usb = usb
        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            pass
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if self.dev is None:
            raise LcdError("LCD %04X:%04X not found. Check the cable, and on Windows make sure the "
                           "device uses the WinUSB driver (Zadig) if the vendor app is not installed."
                           % (VID, PID))
        try:
            cfg = self.dev.get_active_configuration()
        except Exception:
            self.dev.set_configuration()
            cfg = self.dev.get_active_configuration()
        self.ifaces = []
        for intf in cfg:                      # original claims every interface (0x4174e0)
            n = intf.bInterfaceNumber
            try:
                if self.dev.is_kernel_driver_active(n):
                    self.dev.detach_kernel_driver(n)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(self.dev, n)
            self.ifaces.append(n)

    def _is_timeout(self, e):
        return (isinstance(e, getattr(self.usb.core, "USBTimeoutError", ()))
                or getattr(e, "backend_error_code", None) == -7 or e.errno in (errno.ETIMEDOUT, 110, 10060))

    def _is_pipe(self, e):
        return e.errno == errno.EPIPE or getattr(e, "backend_error_code", None) == -9

    def bulk_out(self, ep, data, timeout):
        for _ in range(5):                    # retry on STALL + clear_halt (0x417660..0x417697)
            try:
                return self.dev.write(ep, data, timeout)
            except self.usb.core.USBError as e:
                if not self._is_pipe(e):
                    raise (LcdTimeout if self._is_timeout(e) else LcdError)("bulk OUT failed: %s" % e)
                self.dev.clear_halt(ep)
        raise LcdError("bulk OUT stalled")

    def bulk_in(self, ep, length, timeout):
        for _ in range(5):
            try:
                return bytes(self.dev.read(ep, length, timeout))
            except self.usb.core.USBError as e:
                if not self._is_pipe(e):
                    raise (LcdTimeout if self._is_timeout(e) else LcdError)("bulk IN failed: %s" % e)
                self.dev.clear_halt(ep)
        raise LcdError("bulk IN stalled")

    def drain(self, ep=EP_IN, timeout=50):
        """Discard anything the device still has queued for us (stale CSW from an aborted command)."""
        got = []
        for _ in range(8):
            try:
                got.append(bytes(self.dev.read(ep, 512, timeout)))
            except self.usb.core.USBError:
                break
        return got

    def close(self):
        try:
            for n in self.ifaces:
                self.usb.util.release_interface(self.dev, n)
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass


# --------------------------------------------------------------------------- device
class Lcd:
    def __init__(self, transport=None, handshake=None, settle=None):
        self.t = transport or PyUSBTransport()
        self.tag = 1                          # tag counter starts at 1 (.data 0x7d8040)
        self.width = self.height = None
        if HANDSHAKE if handshake is None else handshake:
            err = None
            try:
                self._query_info()            # first command of every session, as in the original
            except LcdError as e:
                err = e
            time.sleep(SETTLE_S if settle is None else settle)   # Sleep(1000) after info, success or not
            if err:
                self.t.close()
                raise err

    def close(self):
        self.t.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @staticmethod
    def _log(msg):
        if not TRACE:
            return
        line = "[lcd %9.1fms] %s" % ((time.perf_counter() - _T0) * 1000, msg)
        if TRACE_FILE:
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        elif sys.stderr:
            print(line, file=sys.stderr, flush=True)

    # ---- BOT framing
    def _xfer(self, cdb, out=None, in_len=0):
        assert len(cdb) == 16
        tag, self.tag = self.tag, (self.tag + 1) & 0xFFFFFFFF
        n = len(out) if out is not None else in_len
        flags = 0x80 if in_len else 0x00
        cbw = struct.pack("<IIIBBB", CBW_SIG, tag, n, flags, 0, 0x10) + bytes(cdb)
        ctx = "tag=%d cdb=%s dlen=%d" % (tag, bytes(cdb).hex(), n)
        self._log("CBW " + ctx + " flags=%02x" % flags)
        for attempt in range(CBW_RETRIES + 1):
            try:
                self.t.bulk_out(EP_OUT, cbw, T_CBW)
                break
            except LcdTimeout as e:
                self._log("CBW timed out (attempt %d) - device busy?" % (attempt + 1))
                if attempt == CBW_RETRIES:
                    raise LcdTimeout("%s [sending command %s]" % (e, ctx))
                time.sleep(0.1)
        data, data_err = b"", None
        try:
            if out:
                self.t.bulk_out(EP_OUT, bytes(out), T_DATA)
            elif in_len:
                data = self.t.bulk_in(EP_IN, in_len, T_DATA)
                self._log("DATA in=%s" % data.hex())
        except LcdError as e:                 # like the original: always try to read the CSW afterwards,
            data_err = type(e)("%s [data phase of %s]" % (e, ctx))   # otherwise it stays queued and desyncs
        try:
            self._read_csw(tag)
        except LcdError:
            if data_err:
                raise data_err
            raise
        if data_err:
            raise data_err
        return data

    def _read_csw(self, tag):
        """The original checks: 13 bytes received, tag matches, status == 0 (never the 'USBS' signature).
        Extra here: a CSW with the wrong tag is treated as stale and skipped (resync)."""
        seen, waited = [], 0
        for _ in range(4 + CSW_RETRIES):
            try:
                csw = self.t.bulk_in(EP_IN, 13, T_CSW)
            except LcdTimeout:
                if seen:
                    break
                waited += 1
                if waited > CSW_RETRIES:
                    raise
                self._log("CSW timed out (attempt %d) - command still running?" % waited)
                continue
            except LcdError:
                if seen:
                    break
                raise
            self._log("CSW raw=%s" % csw.hex())
            if len(csw) != 13:
                raise LcdError("short CSW (%d bytes): %s" % (len(csw), csw.hex()))
            rtag = struct.unpack_from("<I", csw, 4)[0]
            if rtag == tag or LAX_CSW:
                if csw[12]:
                    raise LcdError("command failed (CSW status %d)" % csw[12])
                return
            seen.append(csw.hex())
        raise LcdError("CSW tag mismatch: expected tag %d (%08x), device sent: %s"
                       % (tag, tag, " | ".join(seen)))

    # ---- commands
    def get_info(self, refresh=False):
        if self.width and not refresh:
            return self.width, self.height
        return self._query_info()

    def _query_info(self):
        c = bytearray(16)
        c[0], c[5] = 0xCD, 0x03
        d = self._xfer(c, in_len=5)
        if len(d) < 4:
            raise LcdError("bad info reply")
        self.width, self.height = d[0] | d[1] << 8, d[2] | d[3] << 8
        return self.width, self.height

    def set_off_mode(self, v):
        c = bytearray(16)
        c[0], c[5], c[6] = 0xCD, 0x04, v & 0xFF
        self._xfer(c)

    def blit_jpeg(self, jpeg):
        if not self.width:
            self.get_info()
        c = bytearray(16)
        c[0], c[5], c[6] = 0xCD, 0x06, 0x30
        struct.pack_into("<HHHH", c, 7, 0, 0, self.width - 1, self.height - 1)
        self._xfer(c, out=jpeg)

    def set_offset(self, x, y):
        c = bytearray(16)
        c[0], c[1] = 0xFE, 0xD1
        struct.pack_into("<HH", c, 8, x & 0xFFFF, y & 0xFFFF)
        self._xfer(c)

    def write_settings(self, work_mode, off_mode, lcd_dir):
        page = bytes([work_mode & 0xFF, off_mode & 0xFF, lcd_dir & 0xFF]) + b"\xff" * (SETTINGS_LEN - 3)
        c = bytearray(16)
        c[0], c[1] = 0xFB, 0xDA
        struct.pack_into(">II", c, 8, 0, SETTINGS_LEN)
        self._xfer(c, out=page)

    def factory_reset(self):
        self.write_settings(0, 1, 0)          # 00 01 00 FF.. (0x4208ef)

    def flash_write(self, blob, progress=None):
        """Erase + write a complete resource (16-byte header + payload) at flash offset 0."""
        total = len(blob)
        if total - 0x10 > MAX_RES:
            raise LcdError("Resource too big (%d > %d bytes, 3.7 MB limit)" % (total - 0x10, MAX_RES))
        sectors = -(-total // SECTOR)
        steps = sectors * 2
        done = 0
        for i in range(sectors):              # 1) erase  (FB D8)
            c = bytearray(16)
            c[0], c[1] = 0xFB, 0xD8
            struct.pack_into(">I", c, 8, i * SECTOR)
            self._xfer(c)
            done += 1
            if progress:
                progress(done, steps)
        for off in range(0, total, SECTOR):   # 2) program (FB D9)
            chunk = blob[off:off + SECTOR]
            c = bytearray(16)
            c[0], c[1] = 0xFB, 0xD9
            struct.pack_into(">II", c, 8, off, len(chunk))
            self._xfer(c, out=chunk)
            done += 1
            if progress:
                progress(done, steps)
