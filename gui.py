#!/usr/bin/env python3
"""
Telegram Video Duplicate Remover - Desktop App (Windows / Mac / Linux)
=====================================================================

A single-window app:

  * enter your Telegram API keys and log in (one click)
  * load all your groups and tick the ones to work on
  * Scan            -> build the index of every video (Phase 1)
  * Review Duplicates -> see each duplicate set and delete with a checkbox
  * Start Watching  -> auto-check every NEW video (Phase 2), with catch-up
                       for anything added while the app / PC was off

Run:  python gui.py
"""

import asyncio
import configparser
import io
import os
import queue
import sys
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

from telethon import TelegramClient, events
from telethon.tl.types import InputMessagesFilterVideo, InputMessagesFilterDocument
from telethon.errors import FloodWaitError

from db import Index
from tgdedup import (is_video, video_record, human_size, VIDEO_EXTS)

try:
    from PIL import Image, ImageTk        # for the video thumbnails in the review list
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

CONFIG = "config.ini"


class DedupApp:
    def __init__(self, root):
        self.root = root
        root.title("Telegram Video Duplicate Remover")
        root.geometry("940x640")
        root.minsize(880, 580)

        self.logq = queue.Queue()
        self.hist_path = os.path.abspath("history.log")   # permanent record of every action
        self._start_history()
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.idx = None
        self.groups = []          # full list of (id, name), sorted by name
        self.displayed = []       # currently shown (after search filter)
        self.checkvars = {}       # group_id -> BooleanVar (persists across filtering)
        self.mode = tk.StringVar(value="name_size")
        self.dry_run = tk.BooleanVar(value=False)   # Preview only: simulate deletes, remove nothing
        self.watching = False
        self._watch_handler = None
        self.cancel = False       # set by STOP button to abort a running scan/copy/delete
        if self.mode.get() not in ("name_size", "name"):
            self.mode.set("name_size")   # 'hash' retired: it needs downloading every file

        self._prefill = self._read_config()
        self._build_ui()

        threading.Thread(target=self._run_loop, daemon=True).start()
        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- config ----------------
    def _read_config(self):
        c = {"api_id": "", "api_hash": "", "session": "dedup_session", "targets": "all"}
        if os.path.exists(CONFIG):
            cp = configparser.ConfigParser(); cp.read(CONFIG)
            if cp.has_section("telegram"):
                c["api_id"] = cp["telegram"].get("api_id", "")
                c["api_hash"] = cp["telegram"].get("api_hash", "")
                c["session"] = cp["telegram"].get("session", "dedup_session")
            if cp.has_section("matching"):
                self.mode = tk.StringVar(value=cp["matching"].get("mode", "name_size"))
        return c

    def _save_config(self):
        cp = configparser.ConfigParser()
        if os.path.exists(CONFIG):
            cp.read(CONFIG)
        if not cp.has_section("telegram"):
            cp.add_section("telegram")
        cp["telegram"]["api_id"] = self.api_id.get().strip()
        cp["telegram"]["api_hash"] = self.api_hash.get().strip()
        cp["telegram"]["session"] = self._prefill["session"]
        if not cp.has_section("groups"):
            cp.add_section("groups")
            cp["groups"]["targets"] = "all"
        if not cp.has_section("matching"):
            cp.add_section("matching")
        cp["matching"]["mode"] = self.mode.get()
        with open(CONFIG, "w") as f:
            cp.write(f)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = dict(padx=6, pady=4)

        # Credentials
        cred = ttk.LabelFrame(self.root, text="1. Telegram account")
        cred.pack(fill="x", **pad)
        ttk.Label(cred, text="API ID:").grid(row=0, column=0, sticky="e", **pad)
        self.api_id = ttk.Entry(cred, width=16)
        self.api_id.insert(0, self._prefill["api_id"]); self.api_id.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cred, text="API Hash:").grid(row=0, column=2, sticky="e", **pad)
        self.api_hash = ttk.Entry(cred, width=38)
        self.api_hash.insert(0, self._prefill["api_hash"]); self.api_hash.grid(row=0, column=3, sticky="w", **pad)
        self.btn_login = ttk.Button(cred, text="Save & Log in", command=self.on_login)
        self.btn_login.grid(row=0, column=4, **pad)
        self.status = ttk.Label(cred, text="Not logged in", foreground="#a00")
        self.status.grid(row=1, column=0, columnspan=5, sticky="w", **pad)

        # Groups
        mid = ttk.Frame(self.root); mid.pack(fill="both", expand=False, **pad)
        gf = ttk.LabelFrame(mid, text="2. Your groups (tick the ones to clean; none ticked = ALL)")
        gf.pack(side="left", fill="both", expand=True)
        # search row
        srow = ttk.Frame(gf); srow.pack(side="top", fill="x", padx=4, pady=(4, 0))
        ttk.Label(srow, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._apply_filter())
        ttk.Entry(srow, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=4)
        # body: scrollable checkbox list + buttons
        body = ttk.Frame(gf); body.pack(side="top", fill="both", expand=True)
        self.gcanvas = tk.Canvas(body, height=170, highlightthickness=0)
        self.gcanvas.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        gsb = ttk.Scrollbar(body, orient="vertical", command=self.gcanvas.yview)
        gsb.pack(side="left", fill="y")
        self.gcanvas.configure(yscrollcommand=gsb.set)
        self.check_inner = ttk.Frame(self.gcanvas)
        self._check_win = self.gcanvas.create_window((0, 0), window=self.check_inner, anchor="nw")
        self.check_inner.bind("<Configure>",
                              lambda e: self.gcanvas.configure(scrollregion=self.gcanvas.bbox("all")))
        self.gcanvas.bind("<Configure>",
                          lambda e: self.gcanvas.itemconfig(self._check_win, width=e.width))
        self.gcanvas.bind_all("<MouseWheel>",
                              lambda e: self.gcanvas.yview_scroll(int(-e.delta / 120), "units"))
        gbtns = ttk.Frame(body); gbtns.pack(side="left", fill="y", padx=4)
        ttk.Button(gbtns, text="Load my groups", command=self.on_load_groups).pack(fill="x", pady=2)
        ttk.Button(gbtns, text="Select all", command=self._check_all).pack(fill="x", pady=2)
        ttk.Button(gbtns, text="Clear", command=self._check_none).pack(fill="x", pady=2)

        # Actions
        af = ttk.LabelFrame(mid, text="3. Actions")
        af.pack(side="left", fill="y", padx=(8, 0))
        ttk.Label(af, text="Match by:").pack(anchor="w", padx=6, pady=(6, 0))
        ttk.Combobox(af, textvariable=self.mode, state="readonly", width=14,
                     values=["name_size", "name"]).pack(padx=6, pady=2)
        # Preview / dry-run: when ticked, deletions are only listed, never performed.
        ttk.Checkbutton(af, text="Preview only (no delete)",
                        variable=self.dry_run, command=self._on_dry_toggle).pack(anchor="w", padx=6, pady=(2, 0))
        self.btn_scan = ttk.Button(af, text="Scan (build index)", command=self.on_scan, state="disabled")
        self.btn_scan.pack(fill="x", padx=6, pady=4)
        self.btn_review = ttk.Button(af, text="Review duplicates", command=self.on_review, state="disabled")
        self.btn_review.pack(fill="x", padx=6, pady=4)
        self.btn_watch = ttk.Button(af, text="Start watching", command=self.on_watch, state="disabled")
        self.btn_watch.pack(fill="x", padx=6, pady=4)
        self.btn_cross = ttk.Button(af, text="Check vs other groups", command=self.on_cross, state="disabled")
        self.btn_cross.pack(fill="x", padx=6, pady=4)
        self.btn_download = ttk.Button(af, text="⬇ Download videos to PC", command=self.on_download, state="disabled")
        self.btn_download.pack(fill="x", padx=6, pady=4)
        self.btn_stop = ttk.Button(af, text="■ STOP", command=self.on_stop, state="disabled")
        self.btn_stop.pack(fill="x", padx=6, pady=(12, 4))

        # Copy videos between groups
        cp = ttk.LabelFrame(self.root, text="4. Copy videos: one group -> another (then removes duplicates)")
        cp.pack(fill="x", **pad)
        ttk.Label(cp, text="From:").grid(row=0, column=0, sticky="e", **pad)
        self.copy_src = ttk.Combobox(cp, state="readonly", width=22)
        self.copy_src.grid(row=0, column=1, **pad)
        ttk.Label(cp, text="To:").grid(row=0, column=2, sticky="e", **pad)
        self.copy_dst = ttk.Combobox(cp, state="readonly", width=22)
        self.copy_dst.grid(row=0, column=3, **pad)
        self.btn_copy = ttk.Button(cp, text="Copy all videos", command=self.on_copy, state="disabled")
        self.btn_copy.grid(row=0, column=4, **pad)
        # Safe batching: copy N videos, pause, then continue - avoids Telegram anti-spam on big moves
        ttk.Label(cp, text="Max per batch:").grid(row=1, column=0, sticky="e", **pad)
        self.copy_limit = ttk.Entry(cp, width=10); self.copy_limit.insert(0, "200")
        self.copy_limit.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(cp, text="Pause (min):").grid(row=1, column=2, sticky="e", **pad)
        self.copy_wait = ttk.Entry(cp, width=10); self.copy_wait.insert(0, "5")
        self.copy_wait.grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(cp, text="(0 = all at once)", foreground="#666").grid(row=1, column=4, sticky="w", **pad)

        # Log
        lf = ttk.LabelFrame(self.root, text="Activity log")
        lf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(lf, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        histbar = ttk.Frame(lf); histbar.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(histbar, text="Every action is saved to history.log").pack(side="left")
        ttk.Button(histbar, text="Open history log", command=self._open_history).pack(side="right")
        self._log("Welcome! Step 1: enter your API ID + Hash and click 'Save & Log in'.")
        self._log(f"Tip: a permanent record of everything is saved to {self.hist_path}")

    # ---------------- threading helpers ----------------
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        def _cb(f):
            try:
                f.result()
            except Exception as e:
                self._log(f"[!] Unexpected error: {e}")
        fut.add_done_callback(_cb)
        return fut

    def _start_history(self):
        """Write a session banner so each run is easy to find in the history file."""
        try:
            with open(self.hist_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== session started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        except Exception:
            pass

    def _log(self, msg):
        self.logq.put(str(msg))
        # persist every line with a timestamp so there is a permanent transaction history
        try:
            with open(self.hist_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
        except Exception:
            pass

    def _open_history(self):
        path = self.hist_path
        if not os.path.exists(path):
            messagebox.showinfo("History log",
                                "No history yet - it's written as soon as the app logs any activity.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)                       # noqa: attribute exists on Windows
            elif sys.platform == "darwin":
                import subprocess; subprocess.Popen(["open", path])
            else:
                import subprocess; subprocess.Popen(["xdg-open", path])
        except Exception:
            messagebox.showinfo("History log", f"Your full history is saved here:\n{path}")

    def _drain_log(self):
        try:
            while True:
                msg = self.logq.get_nowait()
                self.log.config(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _set_status(self, text, ok=True):
        self.root.after(0, lambda: self.status.config(
            text=text, foreground="#070" if ok else "#a00"))

    def _enable_after_login(self):
        def go():
            for b in (self.btn_scan, self.btn_review, self.btn_watch,
                      self.btn_copy, self.btn_cross, self.btn_download):
                b.config(state="normal")
        self.root.after(0, go)

    def _set_busy(self, busy):
        """Enable the STOP button while a long job runs; called from the async thread."""
        self.root.after(0, lambda: self.btn_stop.config(state="normal" if busy else "disabled"))

    def on_stop(self):
        self.cancel = True
        self._log("[*] Stop requested - finishing the current step and halting...")

    def _on_dry_toggle(self):
        if self.dry_run.get():
            self._log("[*] PREVIEW ONLY is ON - deletions will only be LISTED, nothing is removed. "
                      "Great for testing safely.")
        else:
            self._log("[*] Preview only is OFF - deletions are real again (still asks you to confirm).")

    async def _sleep_cancellable(self, seconds, reason=""):
        """Sleep in 1s steps so STOP works and long Telegram waits show a countdown
        instead of looking frozen."""
        seconds = int(seconds) + 1
        if seconds > 5 and reason:
            self._log(f"    {reason} - waiting {seconds}s (this is normal, not frozen). "
                      f"You can press STOP to cancel.")
        left = seconds
        while left > 0:
            if self.cancel:
                return
            step = min(10, left)
            await asyncio.sleep(step)
            left -= step
            if seconds > 20 and left > 0:
                self._log(f"    ...still waiting, {left}s left")

    def gui_prompt(self, title, prompt, secret=False):
        """Ask the user on the GUI thread; block the caller (async thread) for the answer."""
        result = {}
        ev = threading.Event()
        def ask():
            result["v"] = simpledialog.askstring(title, prompt, parent=self.root,
                                                 show="*" if secret else "")
            ev.set()
        self.root.after(0, ask)
        ev.wait()
        return result.get("v")

    # ---------------- Telegram actions ----------------
    async def _ensure_client(self):
        if self.client and self.client.is_connected():
            return True
        try:
            api_id = int(self.api_id.get().strip())
        except ValueError:
            self._log("[!] API ID must be a number."); return False
        api_hash = self.api_hash.get().strip()
        if not api_hash:
            self._log("[!] Please enter your API Hash."); return False
        self.idx = self.idx or Index()
        self.client = TelegramClient(self._prefill["session"], api_id, api_hash)
        try:
            self._log("[*] Connecting to Telegram...")
            await asyncio.wait_for(self.client.connect(), timeout=30)
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            self._set_status("Cannot reach Telegram", ok=False)
            self._log(f"[!] Could not connect to Telegram ({e or 'timed out'}).")
            self._log("    Your network/firewall is most likely blocking Telegram.")
            self._log("    FIX: connect this PC to your phone's hotspot, or turn on a VPN,")
            self._log("         then click 'Save & Log in' again.")
            return False
        except Exception as e:
            self._set_status("Connection error", ok=False)
            self._log(f"[!] Connection error: {e}")
            return False
        self._log("[+] Connected to Telegram.")
        return True

    async def _login(self):
        try:
            self._save_config()
            if not await self._ensure_client():
                return
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                self._set_status(f"Logged in as {me.first_name}", ok=True)
                self._log(f"[+] Already logged in as {me.first_name}.")
                self._enable_after_login(); return
            self._log("[*] Starting login. A box will ask for your phone number next.")
            phone = self.gui_prompt("Login - step 1 of 2",
                                    "Enter your phone number WITH country code\n"
                                    "(example: +966XXXXXXXXX):")
            if not phone:
                self._log("[!] Login cancelled (no phone number entered)."); return
            phone = phone.strip().replace(" ", "")
            if not phone.startswith("+"):
                self._log("[!] Phone must start with + and your country code (e.g. +966...). "
                          "Click 'Save & Log in' to try again.")
                return
            self._log(f"[*] Sending a login code to {phone} - check the Telegram app on your phone "
                      f"(message from 'Telegram').")
            await self.client.send_code_request(phone)
            code = self.gui_prompt("Login - step 2 of 2",
                                   "Enter the code Telegram just sent you\n"
                                   "(it appears inside your Telegram app):")
            if not code:
                self._log("[!] Login cancelled (no code entered)."); return
            try:
                await self.client.sign_in(phone, code.strip())
            except Exception as e:
                if "password" in str(e).lower() or "2fa" in str(e).lower() or "SESSION_PASSWORD" in str(e):
                    pw = self.gui_prompt("Login", "You have two-step verification.\n"
                                                  "Enter your Telegram password:", secret=True)
                    await self.client.sign_in(password=pw)
                else:
                    raise
            me = await self.client.get_me()
            self._set_status(f"Logged in as {me.first_name}", ok=True)
            self._log(f"[+] Logged in as {me.first_name}! You won't need to do this again.")
            self._log("    Next: click 'Load my groups'.")
            self._enable_after_login()
        except Exception as e:
            self._set_status("Login failed", ok=False)
            msg = str(e)
            self._log(f"[!] Login failed: {msg}")
            low = msg.lower()
            if "phone_number_invalid" in low:
                self._log("    -> The phone number format is wrong. Use + and country code.")
            elif "phone_code_invalid" in low or "code" in low:
                self._log("    -> The code was wrong or expired. Click 'Save & Log in' to get a new code.")
            elif "flood" in low:
                self._log("    -> Too many attempts. Please wait a while before trying again.")

    def _apply_filter(self):
        """Rebuild the visible checkbox list from the search box (matches English or Arabic)."""
        q = self.search_var.get().strip().lower()
        self.displayed = [g for g in self.groups if q in g[1].lower()] if q else list(self.groups)
        for w in self.check_inner.winfo_children():
            w.destroy()
        for gid, name in self.displayed:
            var = self.checkvars.setdefault(gid, tk.BooleanVar(value=False))
            ttk.Checkbutton(self.check_inner, text=name, variable=var).pack(anchor="w", fill="x")
        self.gcanvas.yview_moveto(0)

    def _check_all(self):
        for gid, _ in self.displayed:
            self.checkvars.setdefault(gid, tk.BooleanVar()).set(True)

    def _check_none(self):
        for gid, _ in self.displayed:
            if gid in self.checkvars:
                self.checkvars[gid].set(False)

    def _selected_targets(self):
        sel = [gid for gid, var in self.checkvars.items() if var.get()]
        return sel or None  # None => all

    async def _resolve_chats(self):
        ids = self._selected_targets()
        chats = []
        async for d in self.client.iter_dialogs():
            if d.is_group or d.is_channel:
                if ids is None or d.id in ids:
                    chats.append((d.entity, d.id, d.name))
        return chats

    async def _load_groups(self):
        if not await self._ensure_client():
            return
        if not await self.client.is_user_authorized():
            self._log("[!] Please log in first."); return
        self._log("[*] Loading your groups...")
        self.groups = []
        self.checkvars = {}
        seen_ids = set()
        async for d in self.client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            ent = d.entity
            # skip old "legacy" groups that were upgraded to supergroups (they show as a
            # duplicate of the real group), and any deactivated chat
            if getattr(ent, "migrated_to", None) is not None:
                continue
            if getattr(ent, "deactivated", False):
                continue
            if d.id in seen_ids:                 # safety: never list the same group twice
                continue
            seen_ids.add(d.id)
            self.groups.append((d.id, d.name or "(no name)"))
        # sort alphabetically by name (case-insensitive) so Arabic + English are easy to find
        self.groups.sort(key=lambda g: g[1].lower())
        def fill():
            self.search_var.set("")          # clears filter -> shows all
            self._apply_filter()             # builds the checkbox list from the sorted groups
            names = [n for _, n in self.groups]
            self.copy_src["values"] = names
            self.copy_dst["values"] = names
        self.root.after(0, fill)
        self._log(f"[+] Loaded {len(self.groups)} group(s), sorted A-Z. "
                  f"Tick the ones you want, or use Search to find them quickly.")

    async def _scan(self):
        if not await self._ensure_client():
            return
        chats = await self._resolve_chats()
        if not chats:
            self._log("[!] No groups found. Click 'Load my groups' first."); return
        self.idx = self.idx or Index()
        self._log(f"[*] Scanning {len(chats)} group(s) for videos...")
        mode = self.mode.get()
        total = 0
        self.cancel = False
        self._set_busy(True)
        try:
            for entity, gid, title in chats:
                if self.cancel:
                    self._log("[*] Stopped by user."); break
                self.idx.clear_group(gid)  # fresh scan: reflect Telegram exactly, no stale/cached rows
                n = 0; seen = set()
                for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                    try:
                        async for msg in self.client.iter_messages(entity, filter=flt):
                            if self.cancel:
                                break
                            if msg.id in seen or not is_video(msg):
                                continue
                            seen.add(msg.id)
                            self.idx.upsert(video_record(msg, gid, title, mode, False))
                            n += 1
                            if n % 100 == 0:
                                self._log(f"    {title}: {n} videos...")
                    except FloodWaitError as e:
                        await self._sleep_cancellable(e.seconds, "Telegram rate limit")
                total += n
                self._log(f"[+] {title}: {n} videos.")
            scanned_ids = [gid for _, gid, _ in chats]
            dups = self.idx.duplicate_groups(mode, scanned_ids)
            extra = sum(len(g) - 1 for g in dups)
            self._log(f"[+] Scan {'stopped' if self.cancel else 'done'}. {total} videos indexed. "
                      f"{len(dups)} duplicate set(s) -> {extra} removable copy(ies).")
            self._log("    Click 'Review duplicates' to see and delete them.")
        finally:
            self._set_busy(False)

    async def _scan_one(self, entity, gid, title):
        self.idx = self.idx or Index()
        self.idx.clear_group(gid)          # fresh scan of this one group
        mode = self.mode.get(); n = 0; seen = set()
        for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
            try:
                async for msg in self.client.iter_messages(entity, filter=flt):
                    if self.cancel:
                        break
                    if msg.id in seen or not is_video(msg):
                        continue
                    seen.add(msg.id)
                    self.idx.upsert(video_record(msg, gid, title, mode, False))
                    n += 1
            except FloodWaitError as e:
                await self._sleep_cancellable(e.seconds, "Telegram rate limit")
        self._log(f"[+] {title}: {n} videos indexed.")
        return n

    async def _copy(self, src_gid, src_name, dst_gid, dst_name, limit=0, wait_min=0.0):
        if not await self._ensure_client():
            return
        try:
            src_ent = await self.client.get_entity(src_gid)
            dst_ent = await self.client.get_entity(dst_gid)
        except Exception as e:
            self._log(f"[!] Could not open groups: {e}"); return
        self._log(f"[*] Collecting videos in '{src_name}'...")
        ids, seen = [], set()
        for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
            try:
                async for msg in self.client.iter_messages(src_ent, filter=flt):
                    if msg.id in seen or not is_video(msg):
                        continue
                    seen.add(msg.id); ids.append(msg.id)
            except FloodWaitError as e:
                await self._sleep_cancellable(e.seconds, "Telegram rate limit")
        ids.sort()
        total = len(ids)
        if total == 0:
            self._log("[!] No videos found in the source group."); return
        self._log(f"[+] Copying {total} video(s) to '{dst_name}'. "
                  f"For many videos this runs in the background - please keep the app open.")
        self.cancel = False
        self._set_busy(True)
        try:
            done = 0
            BATCH = 30            # smaller sub-batches = smoother, fewer big rate-limit pauses
            chunk = limit if (limit and limit > 0) else total   # videos before a longer cooldown
            if limit and limit > 0:
                self._log(f"[+] Safe mode: copying {chunk} at a time, then pausing {wait_min:g} min "
                          f"before the next batch (avoids Telegram's anti-spam). Keep the app open - "
                          f"it continues automatically until all {total} are done.")
            pos = 0
            while pos < total and not self.cancel:
                end = min(pos + chunk, total)
                for i in range(pos, end, BATCH):
                    if self.cancel:
                        break
                    batch = ids[i:i + BATCH]
                    while not self.cancel:
                        try:
                            await self.client.forward_messages(dst_ent, batch, src_ent)
                            break
                        except FloodWaitError as e:
                            await self._sleep_cancellable(e.seconds, "Telegram rate limit")
                        except Exception as e:
                            self._log(f"    ! batch error, skipping: {e}"); break
                    done += len(batch)
                    self._log(f"    copied {min(done, total)}/{total}...")
                    await self._sleep_cancellable(2, "")       # gentle pacing so Telegram is happy
                pos = end
                # longer cooldown between big batches (only if a limit + wait were set and more remain)
                if pos < total and not self.cancel and wait_min > 0:
                    self._log(f"[*] Batch done ({min(done, total)}/{total}). Pausing {wait_min:g} min before "
                              f"the next batch (it continues on its own - just keep the app open). Press STOP to cancel.")
                    await self._sleep_cancellable(int(wait_min * 60), "Cooldown between batches")
            self._log(f"[+] Copy {'stopped' if self.cancel else 'finished'}: {done} video(s) in '{dst_name}'.")
            if self.cancel:
                return
            # Then apply the duplicate rules on the destination only (as requested).
            self._log("[*] Applying duplicate rules on the destination group...")
            await self._scan_one(dst_ent, dst_gid, dst_name)
            dups = self.idx.duplicate_groups(self.mode.get(), [dst_gid])
            extra = sum(len(g) - 1 for g in dups)
            # tick ONLY the destination so 'Review duplicates' shows just this group
            def tick_dst():
                for var in self.checkvars.values():
                    var.set(False)
                self.checkvars.setdefault(dst_gid, tk.BooleanVar()).set(True)
            self.root.after(0, tick_dst)
            self._log(f"[+] Done. {extra} duplicate copy(ies) detected in '{dst_name}'. "
                      f"'{dst_name}' is now ticked - click 'Review duplicates' to remove them "
                      f"(keeps the oldest).")
        finally:
            self._set_busy(False)

    def _safe_folder(self, name):
        keep = "".join(c if (c.isalnum() or c in " -_.") else "_" for c in (name or "group")).strip()
        return keep[:60] or "group"

    def _video_filename(self, msg, gid):
        """A safe, unique .mp4 filename for a video message (keeps the real name if it has one)."""
        name, ext = None, ".mp4"
        f = getattr(msg, "file", None)
        if f is not None:
            name = f.name
            ext = f.ext or ".mp4"
        if name:
            stem = name.rsplit(".", 1)[0]
            safe = "".join(c if (c.isalnum() or c in " -_()") else "_" for c in stem).strip() or "video"
            return f"{safe[:80]}_{msg.id}{ext}"
        return f"video_{gid}_{msg.id}{ext}"

    async def _download(self, folder):
        if not await self._ensure_client():
            return
        chats = await self._resolve_chats()
        if not chats:
            self._log("[!] No groups selected. Click 'Load my groups' and tick some, then Download."); return
        self.cancel = False
        self._set_busy(True)
        saved_total = 0
        try:
            for entity, gid, title in chats:
                if self.cancel:
                    self._log("[*] Stopped by user."); break
                sub = os.path.join(folder, self._safe_folder(title))
                os.makedirs(sub, exist_ok=True)
                self._log(f"[*] Downloading videos from '{title}' -> {sub}")
                n = 0; skipped = 0; seen = set()
                for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                    try:
                        async for msg in self.client.iter_messages(entity, filter=flt):
                            if self.cancel:
                                break
                            if msg.id in seen or not is_video(msg):
                                continue
                            seen.add(msg.id)
                            path = os.path.join(sub, self._video_filename(msg, gid))
                            size = getattr(getattr(msg, "file", None), "size", None)
                            # resume-friendly: skip files already fully downloaded
                            if os.path.exists(path) and size and os.path.getsize(path) == size:
                                skipped += 1
                                continue
                            while not self.cancel:
                                try:
                                    await self.client.download_media(msg, file=path)
                                    n += 1; saved_total += 1
                                    if n % 10 == 0:
                                        self._log(f"    {title}: {n} downloaded...")
                                    break
                                except FloodWaitError as e:
                                    await self._sleep_cancellable(e.seconds, "Telegram rate limit")
                                except Exception as e:
                                    self._log(f"    ! could not download msg {msg.id}: {e}"); break
                    except FloodWaitError as e:
                        await self._sleep_cancellable(e.seconds, "Telegram rate limit")
                extra = f" ({skipped} already saved)" if skipped else ""
                self._log(f"[+] '{title}': {n} new video(s) saved{extra} -> {sub}")
            self._log(f"[+] Download {'stopped' if self.cancel else 'finished'}: {saved_total} video(s) now on your PC. "
                      f"This folder is your safe backup - copy it to a drive or cloud and Telegram can never touch it.")
        finally:
            self._set_busy(False)

    async def _collect_dups(self):
        if not self.idx:
            self.idx = Index()
        # show duplicates only for the ticked groups (none ticked => all)
        return self.idx.duplicate_groups(self.mode.get(), self._selected_targets())

    async def _delete_msgs(self, items):
        """items: list of (group_id, message_id, group_name)."""
        if self.dry_run.get():
            self._log(f"[PREVIEW] Preview-only mode is ON - NOTHING will be deleted.")
            for gid, mid, gname in items:
                self._log(f"    (preview) would delete msg {mid} in {gname}")
            self._log(f"[PREVIEW] {len(items)} video(s) WOULD be deleted. "
                      f"Untick 'Preview only (no delete)' when you're ready to delete for real.")
            return
        ok = 0
        self.cancel = False
        self._set_busy(True)
        try:
            for gid, mid, gname in items:
                if self.cancel:
                    self._log("[*] Stopped by user - remaining deletions skipped."); break
                try:
                    ent = await self.client.get_entity(gid)
                    await self.client.delete_messages(ent, [mid], revoke=True)
                    self.idx.mark_deleted(gid, mid)
                    ok += 1
                    self._log(f"    ✓ deleted msg {mid} in {gname}")
                except Exception as e:
                    self._log(f"    ✗ msg {mid} in {gname}: {e}")
            self._log(f"[+] Deleted {ok}/{len(items)} duplicate video(s).")
        finally:
            self._set_busy(False)

    async def _incremental_index(self, chats):
        """Catch-up: index videos posted while the app was closed."""
        mode = self.mode.get()
        new = 0
        for entity, gid, title in chats:
            wm = self.idx.max_message_id(gid)
            if wm == 0:
                continue
            for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                try:
                    async for msg in self.client.iter_messages(entity, min_id=wm, filter=flt):
                        if is_video(msg) and not self.idx.has_message(gid, msg.id):
                            self.idx.upsert(video_record(msg, gid, title, mode, False))
                            new += 1
                except FloodWaitError as e:
                    await self._sleep_cancellable(e.seconds, "Telegram rate limit")
        if new:
            self._log(f"[+] Catch-up: indexed {new} video(s) added while the app was off.")
        return new

    async def _start_watch(self):
        if not await self._ensure_client():
            return
        chats = await self._resolve_chats()
        if not chats:
            self._log("[!] No groups to watch. Click 'Load my groups' first."); return
        self.idx = self.idx or Index()
        titles = {gid: t for _, gid, t in chats}
        self._log("[*] Catching up on videos added while the app was off...")
        await self._incremental_index(chats)
        dups = self.idx.duplicate_groups(self.mode.get(), [gid for _, gid, _ in chats])
        if dups:
            self._log(f"[!] {sum(len(g)-1 for g in dups)} duplicate(s) waiting - "
                      f"click 'Review duplicates' to clear them.")

        entities = [e for e, _, _ in chats]

        @self.client.on(events.NewMessage(chats=entities))
        async def handler(event):
            msg = event.message
            if not is_video(msg):
                return
            gid = event.chat_id
            title = titles.get(gid, str(gid))
            rec = video_record(msg, gid, title, self.mode.get(), False)
            matches = self.idx.find_matches(gid, rec["norm_name"], rec["size"], self.mode.get())
            if not matches:
                self.idx.upsert(rec)
                self._log(f"[+] New unique video kept: \"{rec['filename']}\" in {title}")
                return
            self.idx.upsert(rec)
            oldest = matches[0]
            self._log(f"[!] Duplicate posted in {title}: \"{rec['filename']}\" "
                      f"({human_size(rec['size'])}) - already exists in {oldest['group_name']}")
            if self.dry_run.get():
                self._log(f"    (preview) would delete this new copy from {title} "
                          f"(Preview only is ON - kept).")
                return
            do = self._ask_yesno("Duplicate video detected",
                                 f"'{rec['filename']}' already exists.\n\nDelete this new copy from {title}?")
            if do:
                await self._delete_msgs([(gid, msg.id, title)])
            else:
                self._log("    kept.")

        self._watch_handler = handler
        self.watching = True
        self.root.after(0, lambda: self.btn_watch.config(text="Stop watching"))
        self._log(f"[+] Watching {len(entities)} group(s) live. New videos are checked instantly.")

    async def _stop_watch(self):
        if self._watch_handler:
            self.client.remove_event_handler(self._watch_handler)
            self._watch_handler = None
        self.watching = False
        self.root.after(0, lambda: self.btn_watch.config(text="Start watching"))
        self._log("[+] Stopped watching.")

    def _ask_yesno(self, title, text):
        result = {}
        ev = threading.Event()
        def ask():
            result["v"] = messagebox.askyesno(title, text, parent=self.root)
            ev.set()
        self.root.after(0, ask)
        ev.wait()
        return result.get("v", False)

    # ---------------- button callbacks ----------------
    def on_login(self):        self.submit(self._login())
    def on_load_groups(self):  self.submit(self._load_groups())
    def on_scan(self):
        if self.groups and self._selected_targets() is None:
            if not messagebox.askyesno(
                    "No groups ticked",
                    f"You haven't ticked any group, so this will scan ALL "
                    f"{len(self.groups)} of your groups.\n\nContinue with all groups?"):
                self._log("[*] Scan cancelled. Tick the groups you want, then Scan again.")
                return
        self.submit(self._scan())
    def on_watch(self):
        if self.watching:
            self.submit(self._stop_watch())
        else:
            self.submit(self._start_watch())

    def on_copy(self):
        s, d = self.copy_src.current(), self.copy_dst.current()
        if s < 0 or d < 0:
            messagebox.showinfo("Copy videos", "Please pick both a 'From' and a 'To' group.\n"
                                               "Click 'Load my groups' first if the lists are empty.")
            return
        if s == d:
            messagebox.showinfo("Copy videos", "Source and destination must be different groups.")
            return
        src, dst = self.groups[s], self.groups[d]
        try:
            limit = max(0, int(self.copy_limit.get().strip() or "0"))
        except ValueError:
            limit = 0
        try:
            wait_min = max(0.0, float(self.copy_wait.get().strip() or "0"))
        except ValueError:
            wait_min = 0.0
        info = (f"\n\nSafe mode: {limit} videos per batch, "
                f"then a {wait_min:g} min pause, repeating until done."
                if limit > 0 else "\n\nAll videos will be copied in one go.")
        if not messagebox.askyesno(
                "Copy videos",
                f"Copy videos from:\n    {src[1]}\n\nto:\n    {dst[1]}{info}\n\n"
                f"After copying, duplicates in the destination will be detected so you "
                f"can remove them. Continue?"):
            return
        self.submit(self._copy(src[0], src[1], dst[0], dst[1], limit, wait_min))

    def on_download(self):
        if not self.groups:
            messagebox.showinfo("Download videos",
                                "Click 'Load my groups' first, then tick the group(s) to download.")
            return
        folder = filedialog.askdirectory(title="Choose a folder to save your videos into")
        if not folder:
            return
        tgt = self._selected_targets()
        n = len(self.groups) if tgt is None else len(tgt)
        if not messagebox.askyesno(
                "Download videos to PC",
                f"Download ALL videos from {n} group(s) into:\n{folder}\n\n"
                f"Each group gets its own subfolder. This is your safe backup - real .mp4 "
                f"files on your PC that Telegram can never delete.\n\n"
                f"Big collections take time + disk space. You can press STOP anytime, and "
                f"re-running skips files already downloaded. Continue?"):
            return
        self.submit(self._download(folder))

    def on_cross(self):
        if not self.groups:
            messagebox.showinfo("Check vs other groups", "Click 'Load my groups' first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Check one group against others")
        dlg.geometry("560x470")
        names = [n for _, n in self.groups]
        ttk.Label(dlg, text="Delete duplicates FROM this group (the 'variable' group):",
                  ).pack(anchor="w", padx=10, pady=(10, 2))
        var_cb = ttk.Combobox(dlg, state="readonly", values=names, width=48)
        var_cb.pack(anchor="w", padx=10)
        ttk.Label(dlg, text="\nKEEP these groups (the 'static' groups - tick one or more).\n"
                            "Any video in the variable group that also exists here will be\n"
                            "offered for deletion FROM the variable group only:",
                  ).pack(anchor="w", padx=10, pady=(6, 2))
        lb = tk.Listbox(dlg, selectmode="multiple", height=10, exportselection=False)
        for n in names:
            lb.insert("end", n)
        lb.pack(fill="both", expand=True, padx=10, pady=4)

        def go():
            vi = var_cb.current()
            if vi < 0:
                messagebox.showinfo("Pick a group", "Choose the variable group to delete from.")
                return
            static_idx = [i for i in lb.curselection() if i != vi]
            if not static_idx:
                messagebox.showinfo("Pick static groups", "Tick at least one group to keep/compare against.")
                return
            variable = self.groups[vi]
            statics = [self.groups[i] for i in static_idx]
            dlg.destroy()
            if not messagebox.askyesno(
                    "Confirm",
                    f"Find videos in '{variable[1]}' that also exist in "
                    f"{len(statics)} other group(s), to delete from '{variable[1]}'?\n\n"
                    f"The other group(s) will NOT be touched."):
                return
            self.submit(self._cross_check(variable, statics))
        bar = ttk.Frame(dlg); bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Find duplicates", command=go).pack(side="right", padx=10, pady=8)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right", pady=8)

    async def _cross_check(self, variable, statics):
        if not await self._ensure_client():
            return
        self.cancel = False
        self._set_busy(True)
        try:
            v_gid, v_name = variable
            self._log(f"[*] Cross-check: '{v_name}' vs {len(statics)} other group(s). "
                      f"Refreshing the groups first...")
            # fresh scan of the variable group and each static group
            for gid, name in [variable] + list(statics):
                if self.cancel:
                    self._log("[*] Stopped."); return
                try:
                    ent = await self.client.get_entity(gid)
                    await self._scan_one(ent, gid, name)
                except Exception as e:
                    self._log(f"    ! could not read {name}: {e}")
            sets = self.idx.cross_group_duplicates(v_gid, [g for g, _ in statics], self.mode.get())
            self._log(f"[+] Found {len(sets)} video(s) in '{v_name}' that already exist in the "
                      f"other group(s). Opening review - they will be deleted from '{v_name}' only.")
            self.root.after(0, lambda: self._open_review(sets))
        finally:
            self._set_busy(False)

    def on_review(self):
        fut = self.submit(self._collect_dups())
        def wait():
            if fut.done():
                self._open_review(fut.result())
            else:
                self.root.after(120, wait)
        self.root.after(120, wait)

    # ---------------- review window ----------------
    def _open_review(self, dup_groups):
        if not dup_groups:
            messagebox.showinfo("Review duplicates", "No duplicates found. Run a scan first.")
            return
        win = tk.Toplevel(self.root)
        win.title("Review duplicates - keeping the OLDEST, tick copies to delete")
        win.geometry("760x520")
        canvas = tk.Canvas(win, borderwidth=0)
        frame = ttk.Frame(canvas)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        checks = []       # (var, (gid, mid, gname))
        thumb_targets = []  # (label_widget, gid, mid) to load preview images into
        self._thumb_refs = []  # keep PhotoImage refs alive

        def name_of(v):
            return v['filename'] if v['filename'] else "(unnamed video)"

        for gi, g in enumerate(dup_groups, 1):
            keep = g[0]
            box = ttk.LabelFrame(
                frame, text=f"{gi}. {name_of(keep)}  "
                            f"[{human_size(keep['size'])}]  - {len(g)} copies")
            box.pack(fill="x", padx=8, pady=5)
            # KEEP row (with thumbnail)
            krow = ttk.Frame(box); krow.pack(anchor="w", fill="x", padx=6, pady=1)
            klbl = ttk.Label(krow, text="[ ]", width=10, anchor="center")
            klbl.pack(side="left")
            ttk.Label(krow, text=f"KEEP (oldest): {keep['group_name']}  |  {keep['date']}",
                      foreground="#070").pack(side="left", padx=6)
            thumb_targets.append((klbl, keep["group_id"], keep["message_id"]))
            # victim rows (with thumbnail + checkbox)
            for v in g[1:]:
                vrow = ttk.Frame(box); vrow.pack(anchor="w", fill="x", padx=20, pady=1)
                vlbl = ttk.Label(vrow, text="[ ]", width=10, anchor="center")
                vlbl.pack(side="left")
                var = tk.BooleanVar(value=True)
                ttk.Checkbutton(
                    vrow, variable=var,
                    text=f"delete copy in {v['group_name']}  |  {v['date']}  (msg {v['message_id']})"
                ).pack(side="left", padx=4)
                checks.append((var, (v["group_id"], v["message_id"], v["group_name"])))
                thumb_targets.append((vlbl, v["group_id"], v["message_id"]))

        # load thumbnails in the background (only if Pillow + a live client are available)
        if HAVE_PIL and self.client:
            self.submit(self._load_thumbs(thumb_targets))
        else:
            for lbl, _, _ in thumb_targets:
                lbl.config(text="(no preview)")

        bar = ttk.Frame(win); bar.pack(side="bottom", fill="x")
        def do_delete():
            picked = [t for var, t in checks if var.get()]
            if not picked:
                messagebox.showinfo("Nothing selected", "Tick at least one copy to delete."); return
            if self.dry_run.get():
                if not messagebox.askyesno(
                        "Preview only",
                        f"Preview mode is ON.\n\nThis will only LIST the {len(picked)} video(s) that "
                        f"WOULD be deleted in the Activity log - nothing will actually be removed.\n\n"
                        f"Continue with the preview?"):
                    return
            elif not messagebox.askyesno("Confirm", f"Delete {len(picked)} duplicate video(s)? This cannot be undone."):
                return
            win.destroy()
            self.submit(self._delete_msgs(picked))
        del_text = "Preview ticked copies" if self.dry_run.get() else "Delete ticked copies"
        ttk.Button(bar, text=del_text, command=do_delete).pack(side="right", padx=8, pady=6)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", pady=6)
        if HAVE_PIL and self.client:
            ttk.Label(bar, text="loading previews...").pack(side="left", padx=8)

    async def _load_thumbs(self, targets):
        """Download each video's small thumbnail and show it in the review list."""
        entities = {}
        for lbl, gid, mid in targets:
            try:
                ent = entities.get(gid)
                if ent is None:
                    ent = await self.client.get_entity(gid)
                    entities[gid] = ent
                msg = await self.client.get_messages(ent, ids=mid)
                data = await self.client.download_media(msg, file=bytes, thumb=0) if msg else None
                if data:
                    self.root.after(0, lambda l=lbl, d=data: self._apply_thumb(l, d))
                else:
                    self.root.after(0, lambda l=lbl: self._safe_cfg(l, text="(no preview)"))
            except Exception:
                self.root.after(0, lambda l=lbl: self._safe_cfg(l, text="(no preview)"))

    def _safe_cfg(self, lbl, **kw):
        try:
            if lbl.winfo_exists():
                lbl.config(**kw)
        except Exception:
            pass

    def _apply_thumb(self, lbl, data):
        try:
            if not lbl.winfo_exists():
                return
            img = Image.open(io.BytesIO(data))
            img.thumbnail((96, 72))
            photo = ImageTk.PhotoImage(img)
            self._thumb_refs.append(photo)   # keep a reference so it isn't garbage-collected
            lbl.config(image=photo, text="")
        except Exception:
            self._safe_cfg(lbl, text="(no preview)")

    # ---------------- shutdown ----------------
    def _on_close(self):
        try:
            if self.client and self.client.is_connected():
                self.submit(self.client.disconnect())
        except Exception:
            pass
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    DedupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
