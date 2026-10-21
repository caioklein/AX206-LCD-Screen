#!/usr/bin/env python3
"""
webtoscreen.py - open-source clone of "WebToScreen" (WebBrowserV23Port2020.exe) for the
2.1" cooler LCD (USB 2A55:9990), without the AIDA64 / web-cast part.

    python webtoscreen.py gui                       # same layout as the original dialog
    python webtoscreen.py info
    python webtoscreen.py write  cat.gif            # "Write Res": save into the screen's flash
    python webtoscreen.py write  clip.mp4 --activate
    python webtoscreen.py play   clip.mp4 --loop    # "Play Res": stream live from the PC
    python webtoscreen.py settings --work 1 --off 1 --dir 0   # "Write Set"
    python webtoscreen.py reset                     # "Rest Fact"
    python webtoscreen.py nudge --x 10 --y 0
"""
import argparse
import configparser
import os
import queue
import sys
import threading
import time

import lcdproto
import media
from lcdproto import Lcd, LcdError

INI = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "confight.ini")   # same name as original
OFF_MODES = ["Off Lcd", "Play Res"]
WORK_MODES = ["Aida64Rem", "ResPlay"]          # kept for fidelity; 'ResPlay' = play the stored resource
LCD_DIRS = ["Normal", "Rotate90", "Rotate180", "Rotate270"]


# --------------------------------------------------------------------------- confight.ini
def load_cfg(path=INI):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    g = lambda k, d: cp.getint("int", k, fallback=d)
    return {
        "OffMode": g("OffMode", 1), "WorkMode": g("WorkMode", 1), "LcdDir": g("LcdDir", 0),
        "AutoStart": g("AutoStart", 0), "PlayLocalX": g("PlayLocalX", 0), "PlayLocalY": g("PlayLocalY", 0),
        "ResPath": cp.get("str", "ResPath", fallback="").strip(),
    }


def save_cfg(cfg, path=INI):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp["int"] = {k: str(cfg[k]) for k in ("OffMode", "WorkMode", "LcdDir", "AutoStart", "PlayLocalX", "PlayLocalY")}
    cp["str"] = {"ResPath": cfg["ResPath"]}
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        cp.write(f, space_around_delimiters=False)


def set_autostart(on):
    """HKCU\\...\\Run entry (Windows only), like the original 'Auto Start' checkbox."""
    try:
        import winreg
    except ImportError:
        return
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                       winreg.KEY_SET_VALUE)
    if on:
        winreg.SetValueEx(k, "WebToScreenClone", 0, winreg.REG_SZ,
                          '"%s" "%s" gui' % (sys.executable, os.path.abspath(sys.argv[0])))
    else:
        try:
            winreg.DeleteValue(k, "WebToScreenClone")
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- actions
def do_write(path, cfg, fit="cover", raw=False, activate=False, progress=None, lcd=None):
    own = lcd is None
    lcd = lcd or Lcd()
    try:
        w, h = lcd.get_info()
        blob = media.build_resource(path, w, h, fit=fit, raw=raw)
        lcd.flash_write(blob, progress)
        if activate:                                     # play the stored resource by itself
            lcd.write_settings(1, 1, cfg["LcdDir"])
        return len(blob)
    finally:
        if own:
            lcd.close()


def do_play(path, cfg, stop, loop=False, fps=15, fit="cover", hold=True, lcd=None, on_frame=None, cmds=None,
            exit_mode=False):
    own = lcd is None
    lcd = lcd or Lcd()
    try:
        w, h = lcd.get_info()
        # NOTE: the original sends nothing but frames here. CD..04 (OffMode) is only sent when the program
        # exits (WM_QUERYENDSESSION / close handlers) and FE D1 only from the nudge buttons.
        def pump():                                      # run queued commands on THIS connection only
            while cmds is not None and not cmds.empty():
                kind, *args = cmds.get_nowait()
                if kind == "offset":
                    lcd.set_offset(*args)

        while not stop.is_set():
            last = None
            for jpg, dur in media.frames(path, w, h, cfg["LcdDir"], fit, fps):
                if stop.is_set():
                    break
                pump()
                t0 = time.time()
                lcd.blit_jpeg(jpg)
                last = jpg
                if on_frame:
                    on_frame()
                stop.wait(max(0.0, dur - (time.time() - t0)))
            if last is not None and hold and not loop:   # still image: keep refreshing until stopped
                while not stop.is_set():
                    stop.wait(1.0)
                    if not stop.is_set():
                        pump()
                        lcd.blit_jpeg(last)
            if not loop:
                break
    finally:
        if exit_mode:
            exit_off_mode(lcd, cfg)
        if own:
            lcd.close()


def exit_off_mode(lcd, cfg):
    """What the original does when the program ends: tell the screen what to show once the PC is gone."""
    try:
        lcd.set_off_mode(cfg["OffMode"])
    except LcdError:
        pass


# --------------------------------------------------------------------------- GUI
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_cfg()
    root = tk.Tk()
    root.title("WebToScreen (clone)")
    root.resizable(False, False)
    state = {"stop": None, "busy": False, "step": 5, "lcd": None}

    def get_lcd():
        """One session for the whole program run (the original opens the device once at startup)."""
        if state["lcd"] is None:
            state["lcd"] = Lcd()                   # info + 1 s settle happen inside
        return state["lcd"]

    def on_exit():
        if state["stop"]:
            state["stop"].set()
            time.sleep(0.3)
        if state["lcd"] is not None and not state["busy"]:
            exit_off_mode(state["lcd"], cfg)        # like the original's close/QueryEndSession handlers
        drop_lcd()
        root.destroy()

    def drop_lcd():
        if state["lcd"] is not None:
            state["lcd"].close()
            state["lcd"] = None

    status = tk.StringVar(value="Not linked")
    prog = ttk.Progressbar(root, length=420, maximum=100)
    prog.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 2))
    tk.Label(root, textvariable=status, anchor="w").grid(row=1, column=0, columnspan=2, sticky="we", padx=8)

    box = tk.LabelFrame(root, text="Lcd Setting")
    box.grid(row=2, column=0, padx=8, pady=6, sticky="nsew")
    v_off, v_work, v_dir = (tk.StringVar(value=OFF_MODES[cfg["OffMode"] & 1]),
                            tk.StringVar(value=WORK_MODES[cfg["WorkMode"] & 1]),
                            tk.StringVar(value=LCD_DIRS[cfg["LcdDir"] & 3]))
    for r, (label, var, vals) in enumerate((("Off Mode", v_off, OFF_MODES), ("Work Mode", v_work, WORK_MODES),
                                            ("Lcd Dir", v_dir, LCD_DIRS))):
        tk.Label(box, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=2)
        ttk.Combobox(box, textvariable=var, values=vals, state="readonly", width=12).grid(row=r, column=1, padx=4)

    def collect():
        cfg["OffMode"], cfg["WorkMode"], cfg["LcdDir"] = (OFF_MODES.index(v_off.get()),
                                                         WORK_MODES.index(v_work.get()),
                                                         LCD_DIRS.index(v_dir.get()))
        cfg["ResPath"] = v_path.get().strip()
        cfg["AutoStart"] = int(v_auto.get())
        save_cfg(cfg)

    def run(fn, ok_msg):
        if state["busy"]:
            return

        def worker():
            state["busy"] = True
            try:
                fn()
                root.after(0, lambda: status.set(ok_msg))
            except Exception as e:
                drop_lcd()                          # force a fresh open + handshake on the next action
                root.after(0, lambda e=e: (status.set("Failed: %s" % e), messagebox.showerror("Error", str(e))))
            finally:
                state["busy"] = False
        threading.Thread(target=worker, daemon=True).start()

    def write_set():
        collect()
        def f():
            get_lcd().write_settings(cfg["WorkMode"], cfg["OffMode"], cfg["LcdDir"])
        run(f, "Configuration write successful")

    def reset():
        def f():
            get_lcd().factory_reset()
        run(f, "Successful restoration of original factory")
        v_off.set(OFF_MODES[1]); v_work.set(WORK_MODES[0]); v_dir.set(LCD_DIRS[0])
        collect()

    def load_res():
        p = filedialog.askopenfilename(filetypes=[("Res Files", "*.gif *.avi *.jpg *.bmp *.png *.mp4 *.mkv *.mov *.webm"),
                                                  ("All", "*.*")])
        if p:
            v_path.set(p)
            collect()

    def write_res():
        if state["stop"]:
            return messagebox.showwarning("Busy", "Please Stop Res first!!")
        collect()
        def f():
            def pr(d, t):
                root.after(0, lambda: prog.config(value=100.0 * d / t))
            n = do_write(cfg["ResPath"], cfg, progress=pr, lcd=get_lcd())
        run(f, "Write Res finished")

    def play_toggle():
        if state["stop"]:
            state["stop"].set()
            state["stop"] = None
            btn_play.config(text="Play Res")
            return
        collect()
        stop = state["stop"] = threading.Event()
        state["cmds"] = queue.Queue()
        btn_play.config(text="Stop Res")
        def f():
            try:
                do_play(cfg["ResPath"], cfg, stop, loop=True, cmds=state["cmds"], lcd=get_lcd())
            finally:
                root.after(0, lambda: btn_play.config(text="Play Res"))
                state["stop"] = None
        run(f, "Stopped")

    box2 = tk.LabelFrame(root, text="Res Operating")
    box2.grid(row=2, column=1, padx=8, pady=6, sticky="nsew")
    v_path = tk.StringVar(value=cfg["ResPath"])
    tk.Label(box2, text="Res Path").grid(row=0, column=0, sticky="w")
    tk.Entry(box2, textvariable=v_path, width=34).grid(row=0, column=1, columnspan=2, padx=4)
    btn_play = tk.Button(box2, text="Play Res", width=10, command=play_toggle)
    btn_play.grid(row=1, column=0, pady=4)
    tk.Button(box2, text="Write Res", width=10, command=write_res).grid(row=1, column=1)
    tk.Button(box2, text="Load Res", width=10, command=load_res).grid(row=1, column=2)

    def nudge(dx, dy):
        if not state["stop"]:
            return status.set("Nudge works while Res is playing")
        cfg["PlayLocalX"] += dx * state["step"]
        cfg["PlayLocalY"] += dy * state["step"]
        save_cfg(cfg)
        state["cmds"].put(("offset", cfg["PlayLocalX"], cfg["PlayLocalY"]))   # sent by the streaming thread
        status.set("X=%d Y=%d" % (cfg["PlayLocalX"], cfg["PlayLocalY"]))

    pad = tk.Frame(box2)
    pad.grid(row=2, column=0, columnspan=3, pady=4)
    tk.Button(pad, text="Up", width=6, command=lambda: nudge(0, -1)).grid(row=0, column=1)
    tk.Button(pad, text="Left", width=6, command=lambda: nudge(-1, 0)).grid(row=1, column=0)
    def cycle():
        state["step"] = {1: 5, 5: 10, 10: 20, 20: 1}[state["step"]]
        btn_step.config(text="%dPx" % state["step"])
    btn_step = tk.Button(pad, text="5Px", width=6, command=cycle)
    btn_step.grid(row=1, column=1)
    tk.Button(pad, text="Right", width=6, command=lambda: nudge(1, 0)).grid(row=1, column=2)
    tk.Button(pad, text="Down", width=6, command=lambda: nudge(0, 1)).grid(row=2, column=1)

    row = tk.Frame(root)
    row.grid(row=3, column=0, columnspan=2, pady=6)
    tk.Button(row, text="Rest Fact", width=10, command=reset).pack(side="left", padx=4)
    tk.Button(row, text="Write Set", width=10, command=write_set).pack(side="left", padx=4)
    v_auto = tk.BooleanVar(value=bool(cfg["AutoStart"]))
    tk.Checkbutton(row, text="Auto Start", variable=v_auto,
                   command=lambda: (collect(), set_autostart(v_auto.get()))).pack(side="left", padx=12)

    def link():
        def f():
            w, h = get_lcd().get_info()
            root.after(0, lambda: status.set("Link OK=[%dX%d]" % (w, h)))
        run(f, status.get())
    root.after(300, link)
    root.protocol("WM_DELETE_WINDOW", on_exit)
    root.mainloop()


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", action="store_true", help="print every CBW/CSW as hex to stderr")
    ap.add_argument("--lax-csw", action="store_true", help="accept CSWs whose tag differs (status still checked)")
    ap.add_argument("--no-handshake", action="store_true", help="skip the info + 1 s settle the original does first")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("gui")
    sp.add_parser("info")
    w = sp.add_parser("write"); w.add_argument("file")
    w.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    w.add_argument("--raw", action="store_true", help="byte-exact original behaviour (no resize/transcode)")
    w.add_argument("--activate", action="store_true", help="also set WorkMode=ResPlay/OffMode=Play Res")
    p = sp.add_parser("play"); p.add_argument("file")
    p.add_argument("--loop", action="store_true"); p.add_argument("--fps", type=int, default=15)
    p.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s = sp.add_parser("settings")
    s.add_argument("--work", type=int, choices=[0, 1]); s.add_argument("--off", type=int, choices=[0, 1])
    s.add_argument("--dir", type=int, choices=[0, 1, 2, 3])
    sp.add_parser("reset")
    n = sp.add_parser("nudge"); n.add_argument("--x", type=int, default=0); n.add_argument("--y", type=int, default=0)
    a = ap.parse_args(argv)
    lcdproto.TRACE = lcdproto.TRACE or a.trace
    lcdproto.LAX_CSW = lcdproto.LAX_CSW or a.lax_csw
    lcdproto.HANDSHAKE = lcdproto.HANDSHAKE and not a.no_handshake
    cfg = load_cfg()

    if a.cmd == "gui":
        return gui()
    try:
        if a.cmd == "info":
            with Lcd() as l:
                print("Link OK=[%dX%d]" % l.get_info())
        elif a.cmd == "write":
            def pr(d, t):
                print("\r%3d%%" % (100 * d // t), end="", flush=True)
            n_ = do_write(a.file, cfg, a.fit, a.raw, a.activate, pr)
            print("\nwrote %d bytes" % n_)
            cfg["ResPath"] = os.path.abspath(a.file); save_cfg(cfg)
        elif a.cmd == "play":
            stop = threading.Event()
            try:
                do_play(a.file, cfg, stop, a.loop, a.fps, a.fit, exit_mode=True)
            except KeyboardInterrupt:
                stop.set()
        elif a.cmd == "settings":
            for k, v in (("WorkMode", a.work), ("OffMode", a.off), ("LcdDir", a.dir)):
                if v is not None:
                    cfg[k] = v
            with Lcd() as l:
                l.write_settings(cfg["WorkMode"], cfg["OffMode"], cfg["LcdDir"])
            save_cfg(cfg); print("Configuration write successful")
        elif a.cmd == "reset":
            with Lcd() as l:
                l.factory_reset()
            cfg.update(OffMode=1, WorkMode=0, LcdDir=0); save_cfg(cfg)
            print("Successful restoration of original factory")
        elif a.cmd == "nudge":
            cfg["PlayLocalX"], cfg["PlayLocalY"] = a.x, a.y
            with Lcd() as l:
                l.set_offset(a.x, a.y)
            save_cfg(cfg)
    except LcdError as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
