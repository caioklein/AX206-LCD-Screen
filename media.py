"""
media.py - turn images / GIFs / videos into what the LCD wants.

Resource blob written to flash (see build_resource):
    byte 0       type: 1 = raw RGB565 still, 2 = .gif, 3 = .jpg, 4 = .avi     (0x41f826, 0x41fbfe..0x41fc66)
    bytes 1..4   payload size, big-endian                                     (0x41fc6e..0x41fca8)
    bytes 5..15  zero
    bytes 16..   payload (types 2/3/4: the file, unchanged; type 1: W*H*2 bytes RGB565, big-endian)

Live streaming sends JPEG (GDI+ quality 75, 0x4156b8) frames with the blit command.
"""
import io
import operator
import os
import re
import shutil
import struct
import subprocess
import tempfile

from PIL import Image, ImageSequence

from lcdproto import MAX_RES, LcdError, MediaError

STILL_EXT = {".png", ".bmp"}
JPEG_QUALITY = 75


# --------------------------------------------------------------------------- helpers
def fit_image(img, w, h, mode="cover"):
    img = img.convert("RGB")
    if mode == "stretch":
        return img.resize((w, h), Image.LANCZOS)
    sw, sh = img.size
    scale = max(w / sw, h / sh) if mode == "cover" else min(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(img, ((w - nw) // 2, (h - nh) // 2))
    return canvas


_T_R_HI = bytes(i & 0xF8 for i in range(256))
_T_G_HI = bytes(i >> 5 for i in range(256))
_T_G_LO = bytes((i & 0x1C) << 3 for i in range(256))
_T_B_LO = bytes(i >> 3 for i in range(256))


def rgb565_be(img):
    """Standard RGB565, high byte first (vendor app 0x41f882..0x41f8c0): (R>>3)<<11 | (G>>2)<<5 | B>>3.
    Pure Python via byte lookup tables (about 20 ms for 480x480), so no NumPy is needed."""
    data = img.convert("RGB").tobytes()
    r, g, b = data[0::3], data[1::3], data[2::3]
    hi = bytes(map(operator.or_, r.translate(_T_R_HI), g.translate(_T_G_HI)))
    lo = bytes(map(operator.or_, g.translate(_T_G_LO), b.translate(_T_B_LO)))
    out = bytearray(2 * len(hi))
    out[0::2] = hi
    out[1::2] = lo
    return bytes(out)


def jpeg_bytes(img, quality=JPEG_QUALITY):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def header(rtype, size):
    return bytes([rtype]) + struct.pack(">I", size) + bytes(11)


_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # no console flash when running from the windowed exe


def _ffmpeg():
    """ffmpeg from PATH, else the copy bundled by imageio-ffmpeg (shipped inside the .exe)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise MediaError("ffmpeg not found (needed for videos other than .avi)")


_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
_FF = _NOWIN | _BELOW_NORMAL        # ffmpeg runs without a console window and at low CPU priority (games stay smooth)


def _fit_filter(w, h, fit):
    """Scale to the panel. bilinear is ~2-3x cheaper than ffmpeg's default bicubic and fine at 2.1 inches."""
    if fit == "stretch":
        return "scale=%d:%d:flags=bilinear" % (w, h)
    if fit == "contain":
        return ("scale=%d:%d:force_original_aspect_ratio=decrease:flags=bilinear,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black") % (w, h, w, h)
    return "scale=%d:%d:force_original_aspect_ratio=increase:flags=bilinear,crop=%d:%d" % (w, h, w, h)


_ROT = {0: "", 1: ",transpose=1", 2: ",hflip,vflip", 3: ",transpose=2"}     # clockwise, same as _rotate()


def _video_filter(w, h, fit, fps, lcd_dir=0):
    # fps FIRST: frames that will be dropped anyway are never scaled (halves the cost of a 60 fps source)
    return "fps=%d,%s%s" % (fps, _fit_filter(w, h, fit), _ROT[lcd_dir & 3])


def _in_args(hw):
    return ["-threads", "2"] + (["-hwaccel", "auto"] if hw else [])       # 2 decoder threads max; GPU if possible


def _probe_duration(ff, src):
    r = subprocess.run([ff, "-i", src], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, creationflags=_FF)
    mt = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    return int(mt.group(1)) * 3600 + int(mt.group(2)) * 60 + float(mt.group(3)) if mt else None


def _encode_avi(ff, src, out, vf, q, hw, start=None, length=None):
    pre = _in_args(hw) + (["-ss", "%.2f" % start] if start else [])
    post = ["-t", "%.2f" % length] if length else []
    cmd = [ff, "-y", "-v", "error"] + pre + ["-i", src, "-an", "-sn", "-vf", vf, "-c:v", "mjpeg",
                                              "-pix_fmt", "yuvj420p", "-q:v", str(q)] + post + [out]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=_FF)
    if r.returncode and hw:                                            # GPU decode failed: retry in software
        return _encode_avi(ff, src, out, vf, q, False, start, length)
    if r.returncode:
        raise MediaError("ffmpeg failed: " + r.stderr.decode("utf-8", "replace")[-300:])
    return os.path.getsize(out)


# highest JPEG quality number (lower = better) we accept at each frame rate before dropping the frame rate instead
_Q_LIMIT = {20: 13, 15: 13, 12: 18, 10: 24, 8: 24}
_Q_STEPS = (4, 6, 9, 13, 18, 24)


def _transcode_mjpeg_avi(src, w, h, fit="cover", hwaccel=False):
    """MJPEG AVI at panel size that fits the screen's 3.7 MB memory.
    Instead of re-encoding the whole video up to 35 times, sizes are ESTIMATED from short 3 s samples,
    a bisection picks the best (fps, quality) and the full video is encoded once.
    NOTE: the device-side AVI requirements are unknown (see README) - verify on hardware."""
    ff = _ffmpeg()
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "o.avi")
    budget = int(MAX_RES * 0.95)
    dur = _probe_duration(ff, src)

    def vf(fps):
        return _video_filter(w, h, fit, fps)

    def full(fps, q):
        return _encode_avi(ff, src, out, vf(fps), q, hwaccel) <= budget

    def done():
        with open(out, "rb") as f:
            return f.read()

    try:
        if not dur:                                       # unknown length: fall back to trying settings directly
            for fps, q in ((15, 9), (12, 13), (10, 18), (8, 24), (8, 31)):
                if full(fps, q):
                    return done()
            raise MediaError("could not shrink the video under 3.7 MB; use a shorter clip")
        sample = min(3.0, dur)
        start = max(0.0, dur * 0.4 - sample / 2) if dur > 8 else 0.0

        def est(fps, q):                                  # estimated full-length size in bytes
            sz = _encode_avi(ff, src, out, vf(fps), q, hwaccel, start, sample)
            return sz / sample * dur * 1.08

        for fps in (20, 15, 12, 10, 8):
            qs = [q for q in _Q_STEPS if q <= _Q_LIMIT[fps]]
            if est(fps, qs[-1]) > budget:                 # even the lowest accepted quality is too big here
                continue
            lo, hi = 0, len(qs) - 1                       # bisect for the best quality that still fits
            while lo < hi:
                mid = (lo + hi) // 2
                lo, hi = (lo, mid) if est(fps, qs[mid]) <= budget else (mid + 1, hi)
            for q in qs[lo:]:                             # estimates can be off a little: step down if needed
                if full(fps, q):
                    return done()
        if full(8, 31):
            return done()
        raise MediaError("could not shrink the video under 3.7 MB; use a shorter clip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _shrink_gif(path, w, h):
    im = Image.open(path)
    frames, durs = [], []
    for fr in ImageSequence.Iterator(im):
        frames.append(fit_image(fr, w, h).quantize(128))
        durs.append(fr.info.get("duration", 100))
    step = 1
    while True:
        sel = frames[::step]
        d = [sum(durs[i:i + step]) for i in range(0, len(durs), step)]
        buf = io.BytesIO()
        sel[0].save(buf, "GIF", save_all=True, append_images=sel[1:], duration=d, loop=0, optimize=True)
        if buf.tell() <= MAX_RES or len(sel) <= 2:
            return buf.getvalue()
        step += 1


# --------------------------------------------------------------------------- flash resource
def build_resource(path, w, h, fit="cover", raw=False, hwaccel=False):
    """Return the exact blob that 'Write Res' puts into flash.
    raw=True mimics the original byte-for-byte (no resizing / re-encoding of gif/jpg/avi)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in STILL_EXT:
        payload, rtype = rgb565_be(fit_image(Image.open(path), w, h, fit)), 1
    elif ext == ".gif":
        data = open(path, "rb").read()
        if not raw and len(data) > MAX_RES:
            data = _shrink_gif(path, w, h)
        payload, rtype = data, 2
    elif ext in (".jpg", ".jpeg"):
        im = Image.open(path)
        if raw or im.size == (w, h):
            payload = open(path, "rb").read()
        else:
            payload = jpeg_bytes(fit_image(im, w, h, fit), 90)
        rtype = 3
    elif ext == ".avi":
        payload, rtype = open(path, "rb").read(), 4
    else:
        if raw:
            raise MediaError("unsupported type %s (the screen only stores gif/jpg/avi/bmp/png as-is)" % ext)
        payload, rtype = _transcode_mjpeg_avi(path, w, h, fit, hwaccel), 4
    if len(payload) > MAX_RES:
        raise MediaError("File too big for the screen's memory: %.2f MB (limit 3.7 MB). You can still play it live." % (len(payload) / 1048576))
    return header(rtype, len(payload)) + payload


# --------------------------------------------------------------------------- live frames
def _rotate(img, lcd_dir):
    return img.rotate(-90 * (lcd_dir & 3), expand=True) if lcd_dir & 3 else img


class FrameCache:
    """JPEG frames of one media file, kept in memory (spilling to a temp file past 64 MB) so that a looping
    video is decoded ONCE and replayed with almost no CPU afterwards. Gives up (overflow) past `limit` bytes."""
    LIMIT = 600 * 1024 * 1024

    def __init__(self, limit=None):
        self._f = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        self._idx = []
        self._size = 0
        self.limit = limit or self.LIMIT
        self.overflow = False
        self.complete = False

    def __len__(self):
        return len(self._idx)

    def add(self, jpg, dur):
        if self.overflow:
            return
        if self._size + len(jpg) > self.limit:
            self.overflow = True
            self.close()
            return
        self._f.seek(self._size)
        self._f.write(jpg)
        self._idx.append((self._size, len(jpg), dur))
        self._size += len(jpg)

    def frames(self):
        for off, n, dur in self._idx:
            self._f.seek(off)
            yield self._f.read(n), dur

    def close(self):
        try:
            self._f.close()
        except Exception:                                   # noqa: BLE001
            pass


def frames(path, w, h, lcd_dir=0, fit="cover", fps=15, hwaccel=False):
    """Yield (jpeg_bytes, seconds_to_show). Stills yield one frame (caller repeats if wanted)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in STILL_EXT | {".jpg", ".jpeg"}:
        yield jpeg_bytes(fit_image(_rotate(Image.open(path), lcd_dir), w, h, fit)), 1.0
        return
    if ext == ".gif":
        im = Image.open(path)
        for fr in ImageSequence.Iterator(im):
            d = max(0.02, fr.info.get("duration", 100) / 1000.0)
            yield jpeg_bytes(fit_image(_rotate(fr.convert("RGBA"), lcd_dir), w, h, fit)), d
        return
    ff = _ffmpeg()
    vf = _video_filter(w, h, fit, fps, lcd_dir)
    n = w * h * 3
    for hw in ((True, False) if hwaccel else (False,)):
        p = subprocess.Popen([ff, "-v", "error"] + _in_args(hw) + ["-i", path, "-an", "-sn", "-vf", vf, "-f",
                             "rawvideo", "-pix_fmt", "rgb24", "-"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_FF)
        got = 0
        try:
            while True:
                buf = p.stdout.read(n)
                if len(buf) < n:
                    break
                got += 1
                yield jpeg_bytes(Image.frombytes("RGB", (w, h), buf)), 1.0 / fps
        finally:
            p.kill()
            p.wait()
        if got:
            return                                           # played fine (hardware or software)
        # nothing decoded: if GPU decoding was tried, loop once more in software


# --------------------------------------------------------------------------- GUI helper
def thumbnail(path, size=220):
    """Square preview of a media file (first frame for GIFs/videos). Returns a PIL image or None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in STILL_EXT | {".jpg", ".jpeg", ".gif"}:
            return fit_image(Image.open(path), size, size, "cover")
        r = subprocess.run([_ffmpeg(), "-v", "error", "-ss", "0.5", "-i", path, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, creationflags=_NOWIN)
        return fit_image(Image.open(io.BytesIO(r.stdout)), size, size, "cover")
    except Exception:
        return None
