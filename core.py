"""
core.py - settings, user-facing labels and the high-level actions shared by the GUI and the CLI.
"""
import configparser
import os
import queue
import sys
import time

import lcdproto
import media
from lcdproto import Lcd, LcdError, MediaError

APP_NAME = "Appotech AX206 LCD Screen Manager"
APP_VERSION = "1.3"

# Index == the value stored on the screen.
IDLE_MODES = ["Turn screen off", "Show saved media"]       # "Off mode":  what the screen does once the PC app is gone
START_MODES = ["Live from PC", "Saved media"]              # "Work mode": what the screen shows when it powers up
ORIENTATIONS = ["0°", "90°", "180°", "270°"]               # "Lcd dir"
FIT_MODES = [("Fill", "cover"), ("Fit", "contain"), ("Stretch", "stretch")]
FPS_CHOICES = (10, 15, 24)                                  # casting frame rate: lower = less CPU
MEDIA_TYPES = "*.png *.jpg *.jpeg *.bmp *.gif *.avi *.mp4 *.mkv *.mov *.webm *.wmv *.flv"

DEFAULTS = dict(OffMode=1, WorkMode=1, LcdDir=0, AutoStart=0, PlayLocalX=0, PlayLocalY=0,
                ResPath="", Fit="cover", Step=5, Theme="dark", TrayMin=1, Fps=15, HwDecode=1)
INT_KEYS = ("OffMode", "WorkMode", "LcdDir", "AutoStart", "PlayLocalX", "PlayLocalY", "Step", "TrayMin", "Fps", "HwDecode")


# --------------------------------------------------------------------------- settings file
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "AppotechLCD")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.ini")


def load_cfg(path=None):
    path = path or config_path()
    cfg = dict(DEFAULTS)
    cp = configparser.ConfigParser()
    cp.optionxform = str
    if cp.read(path, encoding="utf-8"):
        sec = cp["settings"] if cp.has_section("settings") else {}
        for k in DEFAULTS:
            if k in sec:
                cfg[k] = int(sec[k]) if k in INT_KEYS else sec[k]
        return cfg
    legacy = os.path.join(app_dir(), "confight.ini")          # import the vendor app's settings once
    if os.path.exists(legacy) and cp.read(legacy, encoding="utf-8"):
        for k in ("OffMode", "WorkMode", "LcdDir", "AutoStart", "PlayLocalX", "PlayLocalY"):
            cfg[k] = cp.getint("int", k, fallback=cfg[k])
        cfg["ResPath"] = cp.get("str", "ResPath", fallback="").strip()
    return cfg


def save_cfg(cfg, path=None):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp["settings"] = {k: str(cfg[k]) for k in DEFAULTS}
    with open(path or config_path(), "w", encoding="utf-8") as f:
        cp.write(f)


def set_autostart(on):
    """'Start with Windows': HKCU\\...\\Run entry (no-op on other systems)."""
    try:
        import winreg
    except ImportError:
        return
    if getattr(sys, "frozen", False):
        cmd = '"%s" --minimized' % sys.executable
    else:
        cmd = '"%s" "%s" --minimized' % (sys.executable.replace("python.exe", "pythonw.exe"), os.path.abspath(sys.argv[0]))
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    try:
        if on:
            winreg.SetValueEx(key, "AppotechLCDManager", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "AppotechLCDManager")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# --------------------------------------------------------------------------- actions
def open_lcd(demo=False):
    """New session: info command + 1 s settle happen inside Lcd()."""
    if demo:
        from simulator import MockDevice
        return Lcd(MockDevice(), settle=0.3)
    return Lcd()


def do_write(path, cfg, fit="cover", raw=False, activate=False, progress=None, lcd=None):
    """Save a file into the screen's own memory (persists after power-off)."""
    own = lcd is None
    lcd = lcd or Lcd()
    try:
        w, h = lcd.get_info()
        blob = media.build_resource(path, w, h, fit=fit, raw=raw, hwaccel=bool(cfg.get("HwDecode", 1)))
        lcd.flash_write(blob, progress)
        if activate:
            lcd.write_settings(1, 1, cfg["LcdDir"])
        return len(blob)
    finally:
        if own:
            lcd.close()


def exit_off_mode(lcd, cfg):
    """Tell the screen what to show now that the PC is no longer driving it (the vendor app sends this
    when it exits). With 'Show saved media' the screen goes back to what is stored in its memory."""
    try:
        lcd.set_off_mode(cfg["OffMode"])
        return True
    except LcdError:
        return False


def do_play(path, cfg, stop, loop=False, fps=15, fit="cover", hold=True, lcd=None, on_frame=None, cmds=None,
            exit_mode=False, on_error=None):
    """Cast a file live from the PC until `stop` is set. Nothing is stored on the screen.

    CPU use: the first pass decodes the file and records the encoded JPEG frames in a FrameCache while streaming;
    every later loop replays the cache, so ffmpeg is not running at all in steady state.

    `cmds` is an optional queue.Queue the GUI uses while casting:
        ("offset", x, y)             shift the picture
        ("media", path, fit, fps)    switch to another file / setting WITHOUT stopping (the screen keeps receiving
                                     frames; if the new file cannot be decoded, `on_error(msg)` is called and the
                                     previous file keeps playing)
    """
    own = lcd is None
    lcd = lcd or Lcd()
    fails = 0
    accepted = 0                                         # frames the screen actually took
    failed = False

    def send(jpg):
        nonlocal fails, accepted
        try:
            lcd.blit_jpeg(jpg)
            fails = 0
            accepted += 1
            if on_frame:
                on_frame()
        except LcdError:
            fails += 1                                   # the vendor app tolerates 2 failures, gives up on the 3rd
            if fails >= 3:
                raise

    def media_pending():
        if cmds is None:
            return False
        with cmds.mutex:
            return any(c[0] == "media" for c in cmds.queue)

    def take_commands():
        """Run queued commands on THIS connection; return the newest requested media tuple or None."""
        newest = None
        while cmds is not None:
            try:
                kind, *args = cmds.get_nowait()
            except queue.Empty:
                break
            if kind == "offset":
                lcd.set_offset(*args)
            elif kind == "media":
                newest = tuple(args) + (None,) * (3 - len(args))
        return newest

    def nap(seconds):                                    # sleep, but wake up for stop / a media switch
        end = time.time() + seconds
        while not stop.is_set() and not media_pending():
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))

    try:
        w, h = lcd.get_info()
        cur, prev = (path, fit, fps), None
        while not stop.is_set():
            switched, bad = None, None
            cache = media.FrameCache()
            try:
                while switched is None and bad is None and not stop.is_set():
                    replay = cache.complete
                    recording = not replay and not cache.overflow
                    source = cache.frames() if replay else media.frames(
                        cur[0], w, h, cfg["LcdDir"], cur[1], cur[2], hwaccel=bool(cfg.get("HwDecode", 1)))
                    n, last, natural = 0, None, False
                    try:
                        while True:
                            try:
                                jpg, dur = next(source)
                            except StopIteration:
                                natural = True
                                break
                            except Exception as e:       # noqa: BLE001 - the file could not be decoded
                                bad = e
                                break
                            if stop.is_set():
                                break
                            switched = take_commands()
                            if switched:
                                break
                            t0 = time.time()
                            send(jpg)
                            n, last = n + 1, jpg
                            if recording:
                                cache.add(jpg, dur)
                            nap(dur - (time.time() - t0))
                    finally:
                        source.close()                   # stops ffmpeg when we leave a video early
                    if switched or bad is not None or stop.is_set():
                        break
                    if n == 0:
                        bad = MediaError("Could not read any frames from this file")
                        break
                    if accepted == 0:
                        raise LcdError("The screen did not accept any frames")
                    if n == 1 and hold:                  # a still picture: keep it alive until stopped/switched
                        while not stop.is_set() and switched is None:
                            nap(1.0)
                            if stop.is_set():
                                break
                            switched = take_commands()
                            if switched is None:
                                send(last)
                        break
                    if not loop:
                        return
                    if natural and recording and not cache.overflow and len(cache) > 1:
                        cache.complete = True            # from now on: replay, no decoding
            finally:
                cache.close()
            if bad is not None:
                if prev is None:                         # the very first file: nothing to fall back to
                    raise bad if isinstance(bad, LcdError) else MediaError("Cannot play this file: %s" % bad)
                if on_error:
                    on_error("Cannot play %s (%s). Still casting the previous file." % (os.path.basename(cur[0]), bad))
                cur, prev = prev, None
            elif switched:
                prev, cur = cur, (switched[0], switched[1], switched[2] or cur[2])
    except Exception:
        failed = True
        raise
    finally:
        if exit_mode and not failed:                     # a failed stream must not wait on more USB timeouts
            exit_off_mode(lcd, cfg)
        if own:
            lcd.close()
