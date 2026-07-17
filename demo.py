#!/usr/bin/env python3
"""
Demo driver for the walkthrough video. Runs the REAL GUI but with a fake
Telegram backend + synthetic data, and auto-drives the buttons so the whole
flow can be screen-recorded. Not part of the shipped tool.
"""
import os
os.environ["DEDUP_DEMO"] = "1"
for f in ("demo_index.db",):
    if os.path.exists(f):
        os.remove(f)

import tkinter as tk
from datetime import datetime, timezone, timedelta
from db import Index
import gui as guimod

BASE = datetime(2024, 3, 1, tzinfo=timezone.utc)
GROUPS = [(-1001, "Movies HD"), (-1002, "Series Vault"),
          (-1003, "Documentaries"), (-1004, "Kids Cartoons")]

# (name, size, day, group_index)  -> some are exact duplicates across groups
FAKE_VIDEOS = [
    ("Inception.2010.1080p.mp4", 2_400_000_000, 0, 0),
    ("Inception.2010.1080p.mp4", 2_400_000_000, 12, 1),   # dup of above
    ("Interstellar.4K.mkv",      8_100_000_000, 1, 0),
    ("Interstellar.4K.mkv",      8_100_000_000, 20, 2),    # dup
    ("Interstellar.4K.mkv",      8_100_000_000, 25, 1),    # dup (3 copies)
    ("Planet.Earth.E01.mp4",     1_200_000_000, 2, 2),
    ("Tom.and.Jerry.S01.mp4",      350_000_000, 3, 3),
    ("Tom.and.Jerry.S01.mp4",      350_000_000, 9, 3),     # dup
    ("Dune.Part.Two.mkv",        6_500_000_000, 4, 0),
    ("The.Office.S02E01.mp4",      420_000_000, 5, 1),
]


class DemoApp(guimod.DedupApp):
    def _read_config(self):
        return {"api_id": "39736618", "api_hash": "••••••••••••••••",
                "session": "demo_session", "targets": "all"}

    async def _ensure_client(self):
        self.idx = self.idx or Index("demo_index.db")
        return True

    async def _login(self):
        await self._ensure_client()
        self._set_status("Logged in as Saeed", ok=True)
        self._log("[*] Telegram sent a code to your phone...")
        self._log("[+] Logged in as Saeed. (You only do this once.)")
        self._enable_after_login()

    async def _load_groups(self):
        await self._ensure_client()
        self.groups = list(GROUPS)
        def fill():
            self.glist.delete(0, "end")
            names = []
            for _, n in self.groups:
                self.glist.insert("end", n)
                names.append(n)
            self.copy_src["values"] = names
            self.copy_dst["values"] = names
        self.root.after(0, fill)
        self._log(f"[+] Loaded {len(self.groups)} groups. Tick the ones to clean.")

    async def _resolve_chats(self):
        return [(None, gid, name) for gid, name in self.groups]

    async def _scan(self):
        await self._ensure_client()
        self.idx.conn.execute("DELETE FROM videos"); self.idx.conn.commit()
        self._log("[*] Scanning 4 groups for videos...")
        gname = {gid: n for gid, n in GROUPS}
        mid = 1000
        for name, size, day, gi in FAKE_VIDEOS:
            gid = GROUPS[gi][0]
            mid += 1
            self.idx.upsert({
                "group_id": gid, "group_name": gname[gid], "message_id": mid,
                "filename": name, "norm_name": name.lower(), "size": size,
                "mime_type": "video/mp4", "file_unique_id": str(mid),
                "date": (BASE + timedelta(days=day)).isoformat(),
                "content_hash": None, "status": "kept",
            })
        dups = self.idx.duplicate_groups("name_size")
        extra = sum(len(g) - 1 for g in dups)
        self._log(f"[+] Scan done. {len(FAKE_VIDEOS)} videos indexed.")
        self._log(f"[+] Found {len(dups)} duplicate set(s) -> {extra} removable copies.")
        self._log("    Click 'Review duplicates' to see them.")

    async def _delete_msgs(self, items):
        for gid, mid, gname in items:
            self.idx.mark_deleted(gid, mid)
            self._log(f"    ✓ deleted duplicate in {gname}")
        self._log(f"[+] Deleted {len(items)} duplicate video(s). Groups are cleaner!")

    async def _incremental_index(self, chats):
        self._log("[+] Catch-up: 2 videos were added while the app was off - indexed.")
        return 2

    async def _start_watch(self):
        await self._ensure_client()
        await self._incremental_index(None)
        self.watching = True
        self.root.after(0, lambda: self.btn_watch.config(text="Stop watching"))
        self._log("[+] Watching 4 groups LIVE. Every new video is checked instantly.")
        self._log("    If a duplicate is posted, you'll get a Yes/No delete prompt.")


def driver(app):
    r = app.root
    seq = [
        (900,  lambda: app.on_login()),
        (2600, lambda: app.on_load_groups()),
        (4400, lambda: app.glist.select_set(0, "end")),
        (5200, lambda: app.on_scan()),
        (7200, lambda: app.on_review()),
        (12500, lambda: [w.destroy() for w in r.winfo_children() if isinstance(w, tk.Toplevel)]),
        (13300, lambda: app.on_watch()),
        (16500, lambda: r.destroy()),
    ]
    for delay, fn in seq:
        r.after(delay, fn)


def main():
    root = tk.Tk()
    root.geometry("1000x680+30+10")
    app = DemoApp(root)
    driver(app)
    root.mainloop()


if __name__ == "__main__":
    main()
