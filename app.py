#!/usr/bin/env python3
"""
Appotech AX206 LCD Screen Manager
Send pictures, GIFs and videos to the 2.1" cooler screen, or store them in the screen's own memory.

    app.py [file]        open the manager (optionally with a file pre-selected)
    app.py --demo        try the interface without a screen attached
"""
import argparse
import os
import queue
import sys
import threading
import time

import customtkinter as ctk
from PIL import Image, ImageDraw
from tkinter import filedialog, messagebox

import core
import media
from core import (APP_NAME, APP_VERSION, FIT_MODES, IDLE_MODES, MEDIA_TYPES, ORIENTATIONS, START_MODES)
from lcdproto import LcdError, MediaError

try:                                           # system tray support (optional: without it, minimize works normally)
    import pystray
except Exception:                              # noqa: BLE001
    pystray = None

# --------------------------------------------------------------------------- look & feel   (light, dark)
BG = ("#eef1f6", "#101319")
CARD = ("#ffffff", "#1a1e26")
INSET = ("#e9edf4", "#242934")
TEXT = ("#1a2230", "#e9edf3")
MUTED = ("#667085", "#8a93a3")
ACCENT = ("#2f6df6", "#4d8bff")
ACCENT_SOFT = ("#bcd3ff", "#4d8bff")        # selected chip in segmented controls (readable text in both themes)
ACCENT_HOVER = ("#2559d4", "#6aa0ff")
OK = ("#168a55", "#38c985")
BAD = ("#d92d20", "#f4685e")
WARN = ("#b54708", "#f0a43a")
SECOND = ("#dfe4ee", "#2b313d")
SECOND_HOVER = ("#cfd6e4", "#363d4b")
FONT = "Segoe UI"


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def friendly(e):
    msg = str(e)
    if "not found" in msg and "LCD" in msg:
        return ("The screen was not found.\n\nCheck that the USB cable is connected to the motherboard header, "
                "close any other program that controls the screen, then press Reconnect.")
    return msg


class App(ctk.CTk):
    def __init__(self, demo=False, first_file=None, screenshot=None, theme=None, start_minimized=False):
        self.cfg = core.load_cfg()
        if theme:
            self.cfg["Theme"] = theme
        ctk.set_appearance_mode(self.cfg["Theme"])
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            scale = 1.0
        h = int(min(740, self.winfo_screenheight() / scale - 90))
        self.geometry("1000x%d" % max(h, 560))
        self.minsize(940, 560)
        ico = resource_path(os.path.join("assets", "app.ico"))
        if os.path.exists(ico):
            self.after(250, lambda: self._try_icon(ico))

        self.demo = demo
        self.tray = None
        self._tray_hint = False
        self.lcd = None
        self.busy = False
        self.preview = None                      # (thread, stop_event, command_queue) while casting
        self.ui_q = queue.Queue()
        self.size = None
        self._thumb = None
        self._build()
        self._refresh_controls()
        if first_file and os.path.exists(first_file):
            self.cfg["ResPath"] = os.path.abspath(first_file)
        self._load_path()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Unmap>", self._on_unmap)
        if self.cfg["TrayMin"]:
            self._start_tray()
        if start_minimized:                       # e.g. launched by "Start with Windows"
            self.after(250, self._go_background)
        self.after(50, self._pump)
        self.after(500, self.connect)
        if screenshot:
            self.after(4500, lambda: self._screenshot(screenshot))

    # ------------------------------------------------------------------ helpers
    def _try_icon(self, ico):
        try:
            self.iconbitmap(ico)
        except Exception:
            pass

    def post(self, fn):
        self.ui_q.put(fn)

    def _pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.after(40, self._pump)

    def _screenshot(self, path):
        from PIL import ImageGrab
        self.update()
        x, y, w, h = self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
        self.destroy()

    def set_status(self, text, kind="info"):
        color = {"info": MUTED, "ok": OK, "bad": BAD, "busy": ACCENT}[kind]
        self.status.configure(text=text, text_color=color)

    def set_link(self, state, text):
        color = {"ok": OK, "bad": BAD, "wait": WARN}[state]
        self.link.configure(text="●  " + text, text_color=color)

    # ------------------------------------------------------------------ UI construction
    def _card(self, parent, title, subtitle=None):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 2))
        ctk.CTkLabel(head, text=title, font=font(16, "bold"), text_color=TEXT).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(head, text=subtitle, font=font(12), text_color=MUTED, justify="left",
                         wraplength=400).pack(anchor="w", pady=(2, 0))
        return card

    def _caption(self, parent, text):
        return ctk.CTkLabel(parent, text=text, font=font(11), text_color=MUTED, justify="left", wraplength=400,
                            anchor="w")

    def _segmented(self, parent, values, command=None):
        return ctk.CTkSegmentedButton(parent, values=values, command=command, font=font(12), height=32,
                                      corner_radius=10, fg_color=INSET, selected_color=ACCENT_SOFT,
                                      selected_hover_color=ACCENT_SOFT, unselected_color=INSET,
                                      unselected_hover_color=SECOND_HOVER, text_color=("#1a2230", "#ffffff"))

    def _button(self, parent, text, command, kind="second", height=36, **kw):
        styles = {
            "primary": dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff"),
            "second": dict(fg_color=SECOND, hover_color=SECOND_HOVER, text_color=TEXT),
            "danger": dict(fg_color="transparent", hover_color=SECOND, text_color=BAD, border_width=1,
                           border_color=BAD),
        }[kind]
        return ctk.CTkButton(parent, text=text, command=command, height=height, corner_radius=10,
                             font=font(13, "bold" if kind == "primary" else "normal"), **styles, **kw)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ---- header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 4))
        header.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(titles, text=APP_NAME, font=font(24, "bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(titles, text="A better screen manager for Digital Picture Frames (DPFs) based on the AX206 CPU",
                     font=font(13), text_color=MUTED).pack(anchor="w")
        right = ctk.CTkFrame(header, fg_color=CARD, corner_radius=14)
        right.grid(row=0, column=1, sticky="e")
        self.link = ctk.CTkLabel(right, text="●  Not connected", font=font(13, "bold"), text_color=MUTED)
        self.link.pack(side="left", padx=(16, 10), pady=10)
        self.btn_reconnect = self._button(right, "Reconnect", self.connect, height=30, width=96)
        self.btn_reconnect.pack(side="left", padx=(0, 10), pady=10)

        # ---- body
        body = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=SECOND,
                                      scrollbar_button_hover_color=SECOND_HOVER)   # scrolls on short screens
        body.grid(row=1, column=0, sticky="nsew", padx=(24, 14), pady=6)
        body.grid_columnconfigure((0, 1), weight=1, uniform="col")
        self._build_media(body).grid(row=0, column=0, sticky="new", padx=(0, 10))
        side = ctk.CTkFrame(body, fg_color="transparent")
        side.grid(row=0, column=1, sticky="new", padx=(10, 0))
        side.grid_columnconfigure(0, weight=1)
        self._build_settings(side).grid(row=0, column=0, sticky="ew")
        self._build_position(side).grid(row=1, column=0, sticky="ew", pady=(12, 0))

        # ---- footer
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=26, pady=(2, 12))
        foot.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(foot, text="Starting…", font=font(12), text_color=MUTED, anchor="w",
                                   justify="left", wraplength=400)
        self.status.grid(row=0, column=0, sticky="w")
        opts = ctk.CTkFrame(foot, fg_color="transparent")
        opts.grid(row=0, column=1, sticky="e")
        self.auto_var = ctk.BooleanVar(value=bool(self.cfg["AutoStart"]))
        ctk.CTkCheckBox(opts, text="Start with Windows", variable=self.auto_var, command=self._autostart,
                        font=font(12), text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        corner_radius=6, checkbox_width=18, checkbox_height=18).pack(side="left", padx=(0, 16))
        self.tray_var = ctk.BooleanVar(value=bool(self.cfg["TrayMin"]) and pystray is not None)
        self.chk_tray = ctk.CTkCheckBox(opts, text="Minimize to tray", variable=self.tray_var,
                                        command=self._tray_option, font=font(12), text_color=TEXT, fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, corner_radius=6, checkbox_width=18,
                                        checkbox_height=18, state="normal" if pystray else "disabled")
        self.chk_tray.pack(side="left", padx=(0, 16))
        self.theme_var = ctk.BooleanVar(value=self.cfg["Theme"] == "light")
        ctk.CTkSwitch(opts, text="Light mode", variable=self.theme_var, command=self._theme, font=font(12),
                      text_color=TEXT, progress_color=ACCENT).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(opts, text=("DEMO · " if self.demo else "") + "v" + APP_VERSION, font=font(11),
                     text_color=WARN if self.demo else MUTED).pack(side="left")

    def _build_media(self, parent):
        card = self._card(parent, "Media", "Pick a picture, GIF or video, then store it on the screen or play it with this app "
                                       "(better framerate and larger media available).")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=(10, 0))

        frame = ctk.CTkFrame(inner, width=200, height=200, corner_radius=14, fg_color=INSET)
        frame.grid(row=0, column=0, rowspan=3, sticky="nw")
        frame.pack_propagate(False)
        self.thumb_box = ctk.CTkLabel(frame, text="No file\nselected", text_color=MUTED, font=font(13))
        self.thumb_box.pack(expand=True)
        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.grid(row=0, column=1, sticky="nsew", padx=(16, 0))
        inner.grid_columnconfigure(1, weight=1)
        self.file_label = ctk.CTkLabel(info, text="", font=font(13, "bold"), text_color=TEXT, anchor="w",
                                       justify="left", wraplength=210)
        self.file_label.pack(anchor="w", fill="x")
        self.file_meta = ctk.CTkLabel(info, text="", font=font(11), text_color=MUTED, anchor="w")
        self.file_meta.pack(anchor="w", fill="x", pady=(0, 8))
        self.btn_browse = self._button(info, "Choose file…", self.browse, height=34)
        self.btn_browse.pack(fill="x")
        ctk.CTkLabel(info, text="Picture fit", font=font(12), text_color=MUTED).pack(anchor="w", pady=(14, 4))
        self.seg_fit = self._segmented(info, [n for n, _ in FIT_MODES], self._fit_changed)
        self.seg_fit.set(next((n for n, v in FIT_MODES if v == self.cfg["Fit"]), "Fill"))
        self.seg_fit.pack(fill="x")
        cap = self._caption(info, "Fill crops the edges. Fit keeps the whole picture and adds black bars.")
        cap.configure(wraplength=210)
        cap.pack(anchor="w", pady=(6, 0))

        acts = ctk.CTkFrame(card, fg_color="transparent")
        acts.pack(fill="x", padx=20, pady=(16, 0))
        self.btn_save = self._button(acts, "Save to screen memory", self.save, "primary", height=46)
        self.btn_save.pack(fill="x")
        self._caption(acts, "Stores the file inside the screen. It keeps playing by itself, even when the PC is off "
                            "or this app is closed. Replaces what is stored now.").pack(anchor="w", pady=(5, 10))
        self.btn_preview = self._button(acts, "Play media on screen", self.toggle_preview, height=42)
        self.btn_preview.pack(fill="x")
        self._caption(acts, "Casts the file live from this PC without saving anything. You can pick another file "
                            "while it plays. When you stop, the screen goes back to its stored media."
                      ).pack(anchor="w", pady=(6, 0))

        self.progress_box = ctk.CTkFrame(card, fg_color="transparent")
        self.progress_box.pack(fill="x", padx=20, pady=(12, 14))
        self.progress = ctk.CTkProgressBar(self.progress_box, height=8, corner_radius=4, progress_color=ACCENT,
                                           fg_color=INSET)
        self.progress.set(0)
        self.progress_label = ctk.CTkLabel(self.progress_box, text="", font=font(11), text_color=MUTED, anchor="w")
        self.progress_label.pack(side="bottom", fill="x")
        return card

    def _show_progress(self, on):
        if on:
            self.progress.pack(fill="x", pady=(0, 4), before=self.progress_label)
        else:
            self.progress.pack_forget()
            self.progress_label.configure(text="")

    def _build_settings(self, parent):
        card = self._card(parent, "Screen settings", "Stored in the screen, kept after power-off.")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=(4, 14))

        def row(label, values, current):
            ctk.CTkLabel(inner, text=label, font=font(12, "bold"), text_color=TEXT).pack(anchor="w", pady=(8, 3))
            seg = self._segmented(inner, values)
            seg.set(values[current])
            seg.pack(fill="x")
            return seg

        self.seg_idle = row("When this app is closed", IDLE_MODES, self.cfg["OffMode"] & 1)
        self.seg_start = row("When the screen powers up", START_MODES, self.cfg["WorkMode"] & 1)
        self.seg_rot = row("Rotation", ORIENTATIONS, self.cfg["LcdDir"] & 3)
        btns = ctk.CTkFrame(inner, fg_color="transparent")
        btns.pack(fill="x", pady=(14, 0))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self.btn_apply = self._button(btns, "Apply to screen", self.apply_settings, "primary", height=38)
        self.btn_apply.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_factory = self._button(btns, "Restore factory settings", self.factory_reset, "danger", height=38)
        self.btn_factory.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        return card

    def _build_position(self, parent):
        card = self._card(parent, "Fine-tune position", "Nudge the picture if it is off-centre. Works while casting.")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=(6, 14))
        pad = ctk.CTkFrame(inner, fg_color="transparent")
        pad.pack(side="left")
        self.nudge_btns = []
        for text, r, c, dx, dy in (("▲", 0, 1, 0, -1), ("◀", 1, 0, -1, 0), ("▶", 1, 2, 1, 0), ("▼", 2, 1, 0, 1)):
            b = self._button(pad, text, lambda dx=dx, dy=dy: self.nudge(dx, dy), width=44, height=34)
            b.grid(row=r, column=c, padx=2, pady=2)
            self.nudge_btns.append(b)
        b = self._button(pad, "⟲", lambda: self.nudge(None, None), width=44, height=34)
        b.grid(row=1, column=1, padx=2, pady=2)
        self.nudge_btns.append(b)
        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="left", padx=(22, 0), fill="x", expand=True)
        ctk.CTkLabel(right, text="Step size", font=font(12), text_color=MUTED).pack(anchor="w")
        self.seg_step = self._segmented(right, ["1 px", "5 px", "10 px", "20 px"])
        self.seg_step.set("%d px" % (self.cfg["Step"] if self.cfg["Step"] in (1, 5, 10, 20) else 5))
        self.seg_step.pack(fill="x", pady=(4, 8))
        self.pos_label = ctk.CTkLabel(right, text="", font=font(12, "bold"), text_color=TEXT, anchor="w")
        self.pos_label.pack(anchor="w")
        self._update_pos()
        return card

    # ------------------------------------------------------------------ state
    def _refresh_controls(self):
        previewing = self.preview is not None
        idle = not self.busy and not previewing
        stopping = previewing and self.preview[1].is_set()
        for w in (self.btn_save, self.btn_apply, self.btn_factory, self.btn_reconnect):
            w.configure(state="normal" if idle else "disabled")
        # picking another file while casting switches the stream without stopping it
        self.btn_browse.configure(state="normal" if (idle or (previewing and not stopping and not self.busy))
                                  else "disabled")
        self.btn_preview.configure(
            state="disabled" if (self.busy or stopping) else "normal",
            text="Stopping…" if stopping else ("Stop casting" if previewing else "Play media on screen"),
            fg_color=BAD if previewing else SECOND, hover_color=BAD if previewing else SECOND_HOVER,
            text_color="#ffffff" if previewing else TEXT)
        for b in self.nudge_btns:
            b.configure(state="normal" if (previewing and not stopping) else "disabled")
        self._tray_update()

    def _update_pos(self):
        self.pos_label.configure(text="X: %d    Y: %d" % (self.cfg["PlayLocalX"], self.cfg["PlayLocalY"]))

    def _collect(self):
        self.cfg["OffMode"] = IDLE_MODES.index(self.seg_idle.get())
        self.cfg["WorkMode"] = START_MODES.index(self.seg_start.get())
        self.cfg["LcdDir"] = ORIENTATIONS.index(self.seg_rot.get())
        self.cfg["Step"] = int(self.seg_step.get().split()[0])
        self.cfg["Fit"] = dict(FIT_MODES)[self.seg_fit.get()]
        self.cfg["AutoStart"] = int(self.auto_var.get())
        self.cfg["Theme"] = "light" if self.theme_var.get() else "dark"
        self.cfg["TrayMin"] = int(self.tray_var.get())
        core.save_cfg(self.cfg)

    # ------------------------------------------------------------------ connection / tasks
    def _ensure_lcd(self):
        """Runs on a worker thread. One session for the whole run, like the vendor app."""
        if self.lcd is None:
            self.post(lambda: self.set_link("wait", "Connecting…"))
            self.lcd = core.open_lcd(self.demo)
            w, h = self.lcd.get_info()
            self.size = (w, h)
            self.post(lambda: self.set_link("ok", "Connected  ·  %d × %d" % (w, h)))
        return self.lcd

    def _drop_lcd(self):
        lcd, self.lcd = self.lcd, None
        if lcd is not None:
            try:
                lcd.close()
            except Exception:
                pass

    def _fail(self, e):
        self._drop_lcd()
        msg = friendly(e)
        self.post(lambda: (self.set_link("bad", "Not connected"), self.set_status(str(e).split("\n")[0], "bad"),
                           messagebox.showerror("Something went wrong", msg)))

    def run_task(self, fn, done_msg, busy_msg):
        if self.busy or self.preview:
            return
        self.busy = True
        self._refresh_controls()
        self.set_status(busy_msg, "busy")

        def work():
            try:
                res = fn(self._ensure_lcd())
                msg = done_msg(res) if callable(done_msg) else done_msg
                self.post(lambda: self.set_status(msg, "ok"))
            except Exception as e:                       # noqa: BLE001 - show anything to the user
                self._fail(e)
            finally:
                self.post(self._task_done)
        threading.Thread(target=work, daemon=True).start()

    def _task_done(self):
        self.busy = False
        self._refresh_controls()
        self.after(2200, lambda: None if self.busy else self._show_progress(False))

    def connect(self):
        self._drop_lcd()
        self.run_task(lambda lcd: lcd.get_info(), lambda r: "Connected to the screen (%d × %d)." % r,
                      "Connecting to the screen…")

    # ------------------------------------------------------------------ media
    def browse(self):
        path = filedialog.askopenfilename(
            title="Choose a picture, GIF or video",
            filetypes=[("Pictures, GIFs and videos", MEDIA_TYPES), ("All files", "*.*")])
        if path:
            self._set_media(path)

    def _set_media(self, path):
        self.cfg["ResPath"] = path
        core.save_cfg(self.cfg)
        self._load_path()
        if self.preview and not self.preview[1].is_set():
            self._switch_cast()

    def _switch_cast(self):
        """Tell the running cast to play the selected file (no stop, no flicker back to stored media)."""
        self._collect()
        self.preview[2].put(("media", self.cfg["ResPath"], self.cfg["Fit"]))
        self.set_status("Casting media to the screen: %s" % os.path.basename(self.cfg["ResPath"]), "busy")
        self._tray_update()

    def _fit_changed(self, _=None):
        self._collect()
        if self.preview and not self.preview[1].is_set() and self.cfg["ResPath"]:
            self._switch_cast()                               # re-render the current file with the new fit

    def _load_path(self):
        path = self.cfg["ResPath"]
        if not path or not os.path.exists(path):
            self.file_label.configure(text="No file selected")
            self.file_meta.configure(text="")
            return
        self.file_label.configure(text=os.path.basename(path))
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lstrip(".").upper()
        self.file_meta.configure(text="%s  ·  %.1f MB" % (ext, size / 1048576) if size > 1048576
                                 else "%s  ·  %d KB" % (ext, max(1, size // 1024)))

        def work():
            img = media.thumbnail(path, 180)
            if img is None:
                img = Image.new("RGB", (180, 180), (36, 41, 52))
                d = ImageDraw.Draw(img)
                d.polygon([(70, 55), (70, 125), (130, 90)], fill=(138, 147, 163))
            self.post(lambda: self._set_thumb(img))
        threading.Thread(target=work, daemon=True).start()

    def _set_thumb(self, img):
        self._thumb = ctk.CTkImage(light_image=img, dark_image=img, size=(180, 180))
        self.thumb_box.configure(image=self._thumb, text="")

    def _need_file(self):
        path = self.cfg["ResPath"]
        if not path or not os.path.exists(path):
            messagebox.showinfo("Choose a file first", "Press “Choose file…” and pick a picture, GIF or video.")
            return None
        return path

    def save(self):
        path = self._need_file()
        if not path:
            return
        self._collect()
        fit = self.cfg["Fit"]

        self._show_progress(True)

        def progress(done, total):
            frac = done / total
            self.post(lambda: (self.progress.set(frac),
                               self.progress_label.configure(text="Saving to screen memory…  %d%%" % (frac * 100))))

        def job(lcd):
            self.post(lambda: self.progress_label.configure(text="Preparing file…"))
            return core.do_write(path, self.cfg, fit=fit, progress=progress, lcd=lcd)

        self.run_task(job, lambda n: "Saved to the screen's memory (%.1f MB). Set “When this app is closed” to "
                                     "“Show saved media” to see it after closing." % (n / 1048576),
                      "Saving to the screen's memory — do not unplug the screen…")

    # ------------------------------------------------------------------ casting
    CAST_MSG = "Casting media to the screen. Choose another file to switch without stopping."

    def toggle_preview(self):
        if self.preview:
            self.preview[1].set()
            self.set_status("Stopping…", "busy")
            self._refresh_controls()
            return
        path = self._need_file()
        if not path or self.busy:
            return
        self._collect()
        stop, cmds = threading.Event(), queue.Queue()
        accepted = [0]

        def count():
            accepted[0] += 1

        def warn(msg):                                       # a file picked mid-cast could not be played
            self.post(lambda: (self.set_status(msg, "bad"), messagebox.showwarning("Could not play that file", msg)))

        def work():
            try:
                for attempt in (0, 1):
                    self.post(lambda: self.set_status(
                        "Connecting to the screen…" if self.lcd is None else self.CAST_MSG, "busy"))
                    try:
                        lcd = self._ensure_lcd()
                        self.post(lambda: self.set_status(self.CAST_MSG, "busy"))
                        core.do_play(path, self.cfg, stop, loop=True, fit=self.cfg["Fit"], lcd=lcd, cmds=cmds,
                                     exit_mode=True, on_frame=count, on_error=warn)
                        break
                    except LcdError as e:
                        # The screen may have left PC mode (e.g. after the last cast): reconnect once and retry.
                        if attempt == 0 and accepted[0] == 0 and not stop.is_set() and not isinstance(e, MediaError):
                            self._drop_lcd()
                            continue
                        raise
                self._drop_lcd()          # the stop command took the screen out of PC mode: start fresh next time
                back = ("Casting stopped. The screen is back to its stored media." if self.cfg["OffMode"] == 1
                        else "Casting stopped. The screen is now off.")
                self.post(lambda: self.set_status(back, "ok"))
            except Exception as e:                           # noqa: BLE001
                self._fail(e)
            finally:
                self.post(self._preview_done)
        t = threading.Thread(target=work, daemon=True)
        self.preview = (t, stop, cmds)
        self._refresh_controls()
        self.set_status("Connecting to the screen…" if self.lcd is None else self.CAST_MSG, "busy")
        t.start()

    def _preview_done(self):
        self.preview = None
        self._refresh_controls()

    def nudge(self, dx, dy):
        if not self.preview:
            return
        step = int(self.seg_step.get().split()[0])
        if dx is None:
            self.cfg["PlayLocalX"] = self.cfg["PlayLocalY"] = 0
        else:
            self.cfg["PlayLocalX"] += dx * step
            self.cfg["PlayLocalY"] += dy * step
        self.preview[2].put(("offset", self.cfg["PlayLocalX"], self.cfg["PlayLocalY"]))
        self._update_pos()
        core.save_cfg(self.cfg)

    # ------------------------------------------------------------------ settings
    def apply_settings(self):
        self._collect()
        c = self.cfg
        self.run_task(lambda lcd: lcd.write_settings(c["WorkMode"], c["OffMode"], c["LcdDir"]),
                      "Settings applied to the screen.", "Applying settings…")

    def factory_reset(self):
        if not messagebox.askyesno(
                "Restore factory settings?",
                "This resets the screen's settings to their factory defaults:\n\n"
                "  •  When this app is closed: Show saved media\n"
                "  •  When the screen powers up: Live from PC\n"
                "  •  Rotation: 0°\n\nContinue?"):
            return
        self.seg_idle.set(IDLE_MODES[1])
        self.seg_start.set(START_MODES[0])
        self.seg_rot.set(ORIENTATIONS[0])
        self._collect()
        self.run_task(lambda lcd: lcd.factory_reset(), "Factory settings restored.", "Restoring factory settings…")

    def _autostart(self):
        self._collect()
        try:
            core.set_autostart(self.auto_var.get())
        except OSError as e:
            messagebox.showerror("Could not change startup setting", str(e))

    def _theme(self):
        self._collect()
        ctk.set_appearance_mode(self.cfg["Theme"])

    # ------------------------------------------------------------------ system tray
    def _tray_state_text(self):
        if self.preview:
            return "Casting: %s" % os.path.basename(self.cfg["ResPath"])
        return "Working…" if self.busy else "Not casting"

    def _tray_menu(self):
        can_toggle = lambda item: (not self.busy) and (self.preview is not None or bool(self.cfg["ResPath"]))
        return pystray.Menu(
            pystray.MenuItem("Open", lambda icon, item: self.post(self._show_window), default=True),
            pystray.MenuItem(lambda item: self._tray_state_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: "Stop casting" if self.preview else "Play media on screen",
                             lambda icon, item: self.post(self.toggle_preview), enabled=can_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.post(self._tray_exit)))

    def _start_tray(self):
        if pystray is None or self.tray is not None:
            return
        try:
            img = Image.open(resource_path(os.path.join("assets", "app.png"))).convert("RGBA")
            icon = pystray.Icon("AppotechLCDManager", img, APP_NAME, self._tray_menu())
            icon.run_detached()                   # its own thread; every callback goes through self.post()
            self.tray = icon
            self._tray_update()
        except Exception:                         # noqa: BLE001 - no tray available: minimize behaves normally
            self.tray = None

    def _stop_tray(self):
        tray, self.tray = self.tray, None
        if tray is not None:
            try:
                tray.stop()
            except Exception:                     # noqa: BLE001
                pass

    def _tray_update(self):
        if self.tray is None:
            return
        try:
            self.tray.title = ("%s - %s" % (APP_NAME, self._tray_state_text()))[:120]
            self.tray.update_menu()
        except Exception:                         # noqa: BLE001
            pass

    def _tray_option(self):
        self._collect()
        if self.cfg["TrayMin"]:
            self._start_tray()
            if self.tray is None:                 # could not create it: keep the box honest
                self.tray_var.set(False)
                self._collect()
        else:
            self._show_window()
            self._stop_tray()

    def _on_unmap(self, event):
        if event.widget is self and self.tray is not None and self.cfg["TrayMin"]:
            self.after(80, self._check_minimized)

    def _check_minimized(self):
        try:
            if self.state() == "iconic":          # the user pressed the minimize button
                self._hide_to_tray()
        except Exception:                         # noqa: BLE001
            pass

    def _hide_to_tray(self, notify=True):
        if self.tray is None:
            self._start_tray()
        if self.tray is None:
            self.iconify()                        # no tray available -> ordinary minimize
            return
        self.withdraw()                           # gone from the taskbar; everything keeps running
        if notify and not self._tray_hint:
            self._tray_hint = True
            what = "Still casting in the background." if self.preview else "Still running in the background."
            try:
                self.tray.notify(what + " Click the tray icon to open it.", APP_NAME)
            except Exception:                     # noqa: BLE001
                pass

    def _go_background(self):
        if self.cfg["TrayMin"] and self.tray is not None:
            self._hide_to_tray(notify=False)
        else:
            self.iconify()

    def _show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)         # bring to front even if another window has focus
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _tray_exit(self):
        if self.busy:
            self._show_window()                   # so the "still working" question is visible
        self.on_close()

    # ------------------------------------------------------------------ shutdown
    def destroy(self):
        self._stop_tray()                         # the tray thread would otherwise keep the process alive
        super().destroy()

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                "Still working", "The screen is still being updated. Closing now could leave its stored media "
                                 "incomplete.\n\nClose anyway?"):
            return
        self._collect()
        if self.preview:                                  # the preview thread sends the exit command itself
            self.preview[1].set()
            self.preview[0].join(timeout=6)
        elif self.lcd is not None and not self.busy:
            core.exit_off_mode(self.lcd, self.cfg)        # same as the vendor app does when it exits
        self._drop_lcd()
        self._stop_tray()
        self.destroy()


def selftest(outfile):
    """Checks the things a single-file build can get wrong (dev tool): libusb, ffmpeg, video pipeline."""
    import subprocess
    import tempfile
    lines = []

    def rec(name, ok, detail=""):
        lines.append("%s %s %s" % ("PASS" if ok else "FAIL", name, detail))

    try:
        import libusb_package
        rec("libusb backend loads", libusb_package.get_libusb1_backend() is not None)
    except Exception as e:                                # noqa: BLE001
        rec("libusb backend loads", False, repr(e))
    try:
        exe = media._ffmpeg()
        r = subprocess.run([exe, "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, creationflags=media._NOWIN)
        rec("ffmpeg runs", r.returncode == 0, r.stdout.decode(errors="replace").splitlines()[0][:60])
        tmp = tempfile.mkdtemp()
        mp4 = os.path.join(tmp, "t.mp4")
        subprocess.run([exe, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1.5",
                        "-pix_fmt", "yuv420p", mp4], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=media._NOWIN, check=True)
        rec("video thumbnail", media.thumbnail(mp4, 64) is not None)
        fr = list(media.frames(mp4, 480, 480))
        rec("video frames decode to 480x480 JPEG", len(fr) >= 8 and fr[0][0][:2] == b"\xff\xd8", "%d frames" % len(fr))
        blob = media.build_resource(mp4, 480, 480)
        rec("video -> MJPEG AVI resource (type 4, fits 3.7 MB)", blob[0] == 4 and blob[16:20] == b"RIFF",
            "%.2f MB" % (len(blob) / 1048576))
    except Exception as e:                                # noqa: BLE001
        rec("video pipeline", False, repr(e))
    rec("tray backend loads", pystray is not None and pystray.Icon.__module__.endswith("_win32"),
        getattr(getattr(pystray, "Icon", None), "__module__", "missing"))
    ico = resource_path(os.path.join("assets", "app.ico"))
    rec("bundled icon present", os.path.exists(ico))
    rec("bundled tray image present", os.path.exists(resource_path(os.path.join("assets", "app.png"))))
    text = "\n".join(lines)
    print(text)
    with open(outfile, "w") as f:
        f.write(text + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--selftest", metavar="OUTFILE", help=argparse.SUPPRESS)
    ap.add_argument("file", nargs="?", help="media file to select on start")
    ap.add_argument("--demo", action="store_true", help="simulate the screen (no hardware needed)")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--theme", choices=["dark", "light"], help="override the saved theme for this run")
    ap.add_argument("--trace", action="store_true", help="log USB traffic to stderr")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.selftest)
    if a.trace:
        import lcdproto
        lcdproto.TRACE = True
        if sys.stderr is None:                            # windowed exe: no console, log to a file instead
            lcdproto.TRACE_FILE = os.path.join(os.path.dirname(core.config_path()), "usb_trace.log")
    App(demo=a.demo, first_file=a.file, screenshot=a.screenshot, theme=a.theme,
        start_minimized=a.minimized).mainloop()


if __name__ == "__main__":
    main()
