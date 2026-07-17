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
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog

from telethon import TelegramClient, events
from telethon.tl.types import InputMessagesFilterVideo, InputMessagesFilterDocument
from telethon.errors import FloodWaitError

from db import Index
from tgdedup import (is_video, video_record, human_size, VIDEO_EXTS)

CONFIG = "config.ini"


class DedupApp:
    def __init__(self, root):
        self.root = root
        root.title("Telegram Video Duplicate Remover")
        root.geometry("820x620")
        root.minsize(760, 560)

        self.logq = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.idx = None
        self.groups = []          # list of (id, name)
        self.mode = tk.StringVar(value="name_size")
        self.watching = False
        self._watch_handler = None

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
        self.glist = tk.Listbox(gf, selectmode="multiple", height=9, exportselection=False)
        self.glist.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(gf, orient="vertical", command=self.glist.yview)
        sb.pack(side="left", fill="y"); self.glist.config(yscrollcommand=sb.set)
        gbtns = ttk.Frame(gf); gbtns.pack(side="left", fill="y", padx=4)
        ttk.Button(gbtns, text="Load my groups", command=self.on_load_groups).pack(fill="x", pady=2)
        ttk.Button(gbtns, text="Select all", command=lambda: self.glist.select_set(0, "end")).pack(fill="x", pady=2)
        ttk.Button(gbtns, text="Clear", command=lambda: self.glist.select_clear(0, "end")).pack(fill="x", pady=2)

        # Actions
        af = ttk.LabelFrame(mid, text="3. Actions")
        af.pack(side="left", fill="y", padx=(8, 0))
        ttk.Label(af, text="Match by:").pack(anchor="w", padx=6, pady=(6, 0))
        ttk.Combobox(af, textvariable=self.mode, state="readonly", width=14,
                     values=["name_size", "name", "hash"]).pack(padx=6, pady=2)
        self.btn_scan = ttk.Button(af, text="Scan (build index)", command=self.on_scan, state="disabled")
        self.btn_scan.pack(fill="x", padx=6, pady=4)
        self.btn_review = ttk.Button(af, text="Review duplicates", command=self.on_review, state="disabled")
        self.btn_review.pack(fill="x", padx=6, pady=4)
        self.btn_watch = ttk.Button(af, text="Start watching", command=self.on_watch, state="disabled")
        self.btn_watch.pack(fill="x", padx=6, pady=4)

        # Copy videos between groups
        cp = ttk.LabelFrame(self.root, text="4. Copy videos: one group -> another (then removes duplicates)")
        cp.pack(fill="x", **pad)
        ttk.Label(cp, text="From:").grid(row=0, column=0, sticky="e", **pad)
        self.copy_src = ttk.Combobox(cp, state="readonly", width=26)
        self.copy_src.grid(row=0, column=1, **pad)
        ttk.Label(cp, text="To:").grid(row=0, column=2, sticky="e", **pad)
        self.copy_dst = ttk.Combobox(cp, state="readonly", width=26)
        self.copy_dst.grid(row=0, column=3, **pad)
        self.btn_copy = ttk.Button(cp, text="Copy all videos", command=self.on_copy, state="disabled")
        self.btn_copy.grid(row=0, column=4, **pad)

        # Log
        lf = ttk.LabelFrame(self.root, text="Activity log")
        lf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(lf, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self._log("Welcome! Step 1: enter your API ID + Hash and click 'Save & Log in'.")

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

    def _log(self, msg):
        self.logq.put(str(msg))

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
            for b in (self.btn_scan, self.btn_review, self.btn_watch, self.btn_copy):
                b.config(state="normal")
        self.root.after(0, go)

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

    def _selected_targets(self):
        sel = self.glist.curselection()
        if not sel or not self.groups:
            return None  # None => all
        return [self.groups[i][0] for i in sel]

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
        async for d in self.client.iter_dialogs():
            if d.is_group or d.is_channel:
                self.groups.append((d.id, d.name))
        def fill():
            self.glist.delete(0, "end")
            names = []
            for _, name in self.groups:
                self.glist.insert("end", name)
                names.append(name)
            self.copy_src["values"] = names
            self.copy_dst["values"] = names
        self.root.after(0, fill)
        self._log(f"[+] Loaded {len(self.groups)} group(s). Tick the ones to clean (or leave all unticked = all).")

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
        for entity, gid, title in chats:
            n = 0; seen = set()
            for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                try:
                    async for msg in self.client.iter_messages(entity, filter=flt):
                        if msg.id in seen or not is_video(msg):
                            continue
                        seen.add(msg.id)
                        self.idx.upsert(video_record(msg, gid, title, mode, False))
                        n += 1
                        if n % 100 == 0:
                            self._log(f"    {title}: {n} videos...")
                except FloodWaitError as e:
                    self._log(f"    (rate limited, waiting {e.seconds}s)"); await asyncio.sleep(e.seconds + 1)
            total += n
            self._log(f"[+] {title}: {n} videos.")
        dups = self.idx.duplicate_groups(mode)
        extra = sum(len(g) - 1 for g in dups)
        self._log(f"[+] Scan done. {total} videos indexed. "
                  f"{len(dups)} duplicate set(s) -> {extra} removable copy(ies).")
        self._log("    Click 'Review duplicates' to see and delete them.")

    async def _scan_one(self, entity, gid, title):
        self.idx = self.idx or Index()
        mode = self.mode.get(); n = 0; seen = set()
        for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
            try:
                async for msg in self.client.iter_messages(entity, filter=flt):
                    if msg.id in seen or not is_video(msg):
                        continue
                    seen.add(msg.id)
                    self.idx.upsert(video_record(msg, gid, title, mode, False))
                    n += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
        self._log(f"[+] {title}: {n} videos indexed.")
        return n

    async def _copy(self, src_gid, src_name, dst_gid, dst_name):
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
                await asyncio.sleep(e.seconds + 1)
        ids.sort()
        total = len(ids)
        if total == 0:
            self._log("[!] No videos found in the source group."); return
        self._log(f"[+] Copying {total} video(s) to '{dst_name}'. "
                  f"For many videos this runs in the background - please keep the app open.")
        done = 0
        for i in range(0, total, 100):                 # Telegram allows up to 100 per forward
            batch = ids[i:i + 100]
            while True:
                try:
                    await self.client.forward_messages(dst_ent, batch, src_ent)
                    break
                except FloodWaitError as e:
                    self._log(f"    (Telegram pacing: waiting {e.seconds}s, this is normal)")
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    self._log(f"    ! batch error, skipping: {e}"); break
            done += len(batch)
            self._log(f"    copied {done}/{total}...")
            await asyncio.sleep(1.5)                    # gentle pacing so Telegram is happy
        self._log(f"[+] Copy finished: {done} video(s) now in '{dst_name}'.")
        # Then apply the duplicate rules on the destination (as requested).
        self._log("[*] Applying duplicate rules on the destination group...")
        await self._scan_one(dst_ent, dst_gid, dst_name)
        dups = self.idx.duplicate_groups(self.mode.get())
        extra = sum(len(g) - 1 for g in dups)
        self._log(f"[+] Done. {extra} duplicate copy(ies) detected - "
                  f"click 'Review duplicates' to remove them (keeps the oldest).")

    async def _collect_dups(self):
        if not self.idx:
            self.idx = Index()
        return self.idx.duplicate_groups(self.mode.get())

    async def _delete_msgs(self, items):
        """items: list of (group_id, message_id, group_name)."""
        ok = 0
        for gid, mid, gname in items:
            try:
                ent = await self.client.get_entity(gid)
                await self.client.delete_messages(ent, [mid], revoke=True)
                self.idx.mark_deleted(gid, mid)
                ok += 1
                self._log(f"    ✓ deleted msg {mid} in {gname}")
            except Exception as e:
                self._log(f"    ✗ msg {mid} in {gname}: {e}")
        self._log(f"[+] Deleted {ok}/{len(items)} duplicate video(s).")

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
                    await asyncio.sleep(e.seconds + 1)
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
        dups = self.idx.duplicate_groups(self.mode.get())
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
            matches = self.idx.find_matches(rec["norm_name"], rec["size"], self.mode.get())
            if not matches:
                self.idx.upsert(rec)
                self._log(f"[+] New unique video kept: \"{rec['filename']}\" in {title}")
                return
            self.idx.upsert(rec)
            oldest = matches[0]
            self._log(f"[!] Duplicate posted in {title}: \"{rec['filename']}\" "
                      f"({human_size(rec['size'])}) - already exists in {oldest['group_name']}")
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
    def on_scan(self):         self.submit(self._scan())
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
        if not messagebox.askyesno(
                "Copy videos",
                f"Copy ALL videos from:\n    {src[1]}\n\nto:\n    {dst[1]}\n\n"
                f"After copying, duplicates in the destination will be detected so you "
                f"can remove them. Continue?"):
            return
        self.submit(self._copy(src[0], src[1], dst[0], dst[1]))

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

        checks = []  # (var, (gid, mid, gname))
        for gi, g in enumerate(dup_groups, 1):
            keep = g[0]
            box = ttk.LabelFrame(
                frame, text=f"{gi}. {keep['filename'] or '(no name)'}  "
                            f"[{human_size(keep['size'])}]  - {len(g)} copies")
            box.pack(fill="x", padx=8, pady=5)
            ttk.Label(box, text=f"KEEP (oldest): {keep['group_name']}  |  {keep['date']}",
                      foreground="#070").pack(anchor="w", padx=6)
            for v in g[1:]:
                var = tk.BooleanVar(value=True)
                ttk.Checkbutton(
                    box, variable=var,
                    text=f"delete copy in {v['group_name']}  |  {v['date']}  (msg {v['message_id']})"
                ).pack(anchor="w", padx=20)
                checks.append((var, (v["group_id"], v["message_id"], v["group_name"])))

        bar = ttk.Frame(win); bar.pack(side="bottom", fill="x")
        def do_delete():
            picked = [t for var, t in checks if var.get()]
            if not picked:
                messagebox.showinfo("Nothing selected", "Tick at least one copy to delete."); return
            if not messagebox.askyesno("Confirm", f"Delete {len(picked)} duplicate video(s)? This cannot be undone."):
                return
            win.destroy()
            self.submit(self._delete_msgs(picked))
        ttk.Button(bar, text=f"Delete ticked copies", command=do_delete).pack(side="right", padx=8, pady=6)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", pady=6)

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
