#!/usr/bin/env python3
"""
cli.py - command-line access to the Appotech AX206 LCD screen (same engine as the GUI).

    python cli.py info
    python cli.py save  cat.gif                 # store in the screen's memory
    python cli.py save  clip.mp4 --activate     # ...and make the screen play it on power-up
    python cli.py preview clip.mp4 --loop       # stream live from the PC (Ctrl+C to stop)
    python cli.py settings --idle 1 --start 1 --rotation 0
    python cli.py factory-reset
    python cli.py nudge --x 10 --y 0
Add --trace before the command to print every USB frame (useful for bug reports).
"""
import argparse
import os
import sys
import threading

import core
import lcdproto
from core import Lcd, LcdError


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", action="store_true", help="print every USB command/reply as hex")
    ap.add_argument("--lax-csw", action="store_true", help="accept replies whose tag differs (status still checked)")
    ap.add_argument("--no-handshake", action="store_true", help="skip the info command + 1 s wait at connect")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("info", help="show the screen resolution")
    s = sp.add_parser("save", help="store a file in the screen's memory")
    s.add_argument("file")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s.add_argument("--raw", action="store_true", help="copy gif/jpg/avi byte-for-byte (no conversion)")
    s.add_argument("--activate", action="store_true", help="also set the screen to play saved media on power-up")
    p = sp.add_parser("preview", help="stream a file live (nothing is stored)")
    p.add_argument("file")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    t = sp.add_parser("settings", help="write the screen's settings")
    t.add_argument("--idle", type=int, choices=[0, 1], help="when the app is closed: 0 = screen off, 1 = saved media")
    t.add_argument("--start", type=int, choices=[0, 1], help="at power-up: 0 = live from PC, 1 = saved media")
    t.add_argument("--rotation", type=int, choices=[0, 1, 2, 3], help="0, 90, 180, 270 degrees")
    sp.add_parser("factory-reset", help="restore the screen's factory settings")
    n = sp.add_parser("nudge", help="shift the picture while previewing")
    n.add_argument("--x", type=int, default=0)
    n.add_argument("--y", type=int, default=0)
    a = ap.parse_args(argv)

    lcdproto.TRACE = lcdproto.TRACE or a.trace
    lcdproto.LAX_CSW = lcdproto.LAX_CSW or a.lax_csw
    lcdproto.HANDSHAKE = lcdproto.HANDSHAKE and not a.no_handshake
    cfg = core.load_cfg()
    try:
        if a.cmd == "info":
            with Lcd() as lcd:
                print("Connected: %d x %d" % lcd.get_info())
        elif a.cmd == "save":
            def pr(d, tot):
                print("\r  saving... %3d%%" % (100 * d // tot), end="", flush=True)
            n_ = core.do_write(a.file, cfg, a.fit, a.raw, a.activate, pr)
            print("\n  saved %.2f MB to the screen's memory" % (n_ / 1048576))
            cfg["ResPath"] = os.path.abspath(a.file)
            core.save_cfg(cfg)
        elif a.cmd == "preview":
            stop = threading.Event()
            try:
                core.do_play(a.file, cfg, stop, a.loop, a.fps, a.fit, exit_mode=True)
            except KeyboardInterrupt:
                stop.set()
        elif a.cmd == "settings":
            for key, val in (("OffMode", a.idle), ("WorkMode", a.start), ("LcdDir", a.rotation)):
                if val is not None:
                    cfg[key] = val
            with Lcd() as lcd:
                lcd.write_settings(cfg["WorkMode"], cfg["OffMode"], cfg["LcdDir"])
            core.save_cfg(cfg)
            print("Settings applied")
        elif a.cmd == "factory-reset":
            with Lcd() as lcd:
                lcd.factory_reset()
            cfg.update(OffMode=1, WorkMode=0, LcdDir=0)
            core.save_cfg(cfg)
            print("Factory settings restored")
        elif a.cmd == "nudge":
            cfg["PlayLocalX"], cfg["PlayLocalY"] = a.x, a.y
            with Lcd() as lcd:
                lcd.set_offset(a.x, a.y)
            core.save_cfg(cfg)
    except LcdError as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
