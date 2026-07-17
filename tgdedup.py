#!/usr/bin/env python3
"""
Telegram Video Duplicate Remover
================================

Finds and removes duplicate VIDEO files across your Telegram groups.

Workflow
--------
  1.  python tgdedup.py login          # one-time: log into Telegram
  2.  python tgdedup.py groups         # (optional) list your groups
  3.  python tgdedup.py scan           # Phase 1: build the index of all videos
  4.  python tgdedup.py dedup          # review & delete duplicates already in stock
  5.  python tgdedup.py watch          # Phase 2: auto-check every NEW video

Matching, confirmation and "keep the oldest" behaviour are all controlled from
config.ini.  Nothing is ever deleted without a  y/n  confirmation (unless you
explicitly pass --auto).
"""

import argparse
import asyncio
import configparser
import hashlib
import os
import sys
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import (
    InputMessagesFilterVideo,
    InputMessagesFilterDocument,
)
from telethon.errors import FloodWaitError

from db import Index

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".vob", ".ogv", ".mts",
}

# --------------------------------------------------------------------------- #
#  Config helpers
# --------------------------------------------------------------------------- #

def load_config(path="config.ini"):
    if not os.path.exists(path):
        sys.exit(f"[!] {path} not found. Copy config.example.ini to config.ini "
                 f"and fill in your api_id / api_hash.")
    cfg = configparser.ConfigParser()
    cfg.read(path)
    try:
        api_id = int(cfg["telegram"]["api_id"])
        api_hash = cfg["telegram"]["api_hash"].strip()
    except (KeyError, ValueError):
        sys.exit("[!] api_id / api_hash missing or invalid in config.ini")
    if not api_hash or api_hash == "your_api_hash_here":
        sys.exit("[!] Please set a real api_hash in config.ini")
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "session": cfg["telegram"].get("session", "dedup_session"),
        "targets": [t.strip() for t in cfg["groups"].get("targets", "all").split(",") if t.strip()],
        "mode": cfg["matching"].get("mode", "name_size").strip() if cfg.has_section("matching") else "name_size",
        "compare_unnamed_by_size": cfg.getboolean("matching", "compare_unnamed_by_size", fallback=False)
                                    if cfg.has_section("matching") else False,
    }


def make_client(cfg):
    return TelegramClient(cfg["session"], cfg["api_id"], cfg["api_hash"])


# --------------------------------------------------------------------------- #
#  Small utilities
# --------------------------------------------------------------------------- #

def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def norm(name):
    return name.strip().lower() if name else ""


def is_video(message):
    """True if the message carries a video file."""
    f = message.file
    if not f:
        return False
    if getattr(message, "video", None):
        return True
    mime = (f.mime_type or "").lower()
    if mime.startswith("video/"):
        return True
    ext = os.path.splitext((f.name or "").lower())[1]
    return ext in VIDEO_EXTS


def video_record(message, group_id, group_name, mode, compare_unnamed_by_size):
    """Build a dict row for the index from a Telethon message."""
    f = message.file
    name = f.name or ""
    n = norm(name)
    # For name-less videos, fall back to the size as the match key when allowed,
    # so two unnamed identical-size files can still be paired.
    if not n and compare_unnamed_by_size and f.size:
        n = f"__unnamed__{f.size}"
    date = message.date
    if date and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    fuid = None
    if message.document:
        # A stable per-file identifier Telegram assigns; equal for identical uploads.
        fuid = str(message.document.id)
    return {
        "group_id": group_id,
        "group_name": group_name,
        "message_id": message.id,
        "filename": name,
        "norm_name": n,
        "size": f.size,
        "mime_type": f.mime_type,
        "file_unique_id": fuid,
        "date": date.isoformat() if date else None,
        "content_hash": None,
        "status": "kept",
    }


async def resolve_targets(client, targets):
    """Turn the config 'targets' list into a list of (entity, id, title)."""
    result = []
    if len(targets) == 1 and targets[0].lower() == "all":
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                result.append((dialog.entity, dialog.id, dialog.name))
        return result
    for t in targets:
        try:
            ent = await client.get_entity(int(t) if t.lstrip("-").isdigit() else t)
            title = getattr(ent, "title", None) or getattr(ent, "username", str(t))
            result.append((ent, ent.id, title))
        except Exception as e:
            print(f"[!] Could not resolve target '{t}': {e}")
    return result


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #

async def cmd_login(cfg):
    client = make_client(cfg)
    await client.start()                       # prompts phone + code (+2FA) if needed
    me = await client.get_me()
    print(f"[+] Logged in as {me.first_name} (@{me.username or me.id}).")
    print("    Session saved. You won't need to log in again next time.")
    await client.disconnect()


async def cmd_groups(cfg):
    client = make_client(cfg)
    await client.start()
    print("Your groups / channels:\n")
    print(f"{'ID':>16}   Title")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"{dialog.id:>16}   {dialog.name}")
    await client.disconnect()


async def _iter_videos(client, entity):
    """Yield every video message in a chat, using media filters to stay fast."""
    seen = set()
    for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
        while True:
            try:
                async for msg in client.iter_messages(entity, filter=flt):
                    if msg.id in seen or not is_video(msg):
                        continue
                    seen.add(msg.id)
                    yield msg
                break
            except FloodWaitError as e:
                print(f"    (rate limited by Telegram, waiting {e.seconds}s...)")
                await asyncio.sleep(e.seconds + 1)


async def cmd_scan(cfg):
    client = make_client(cfg)
    await client.start()
    idx = Index()
    chats = await resolve_targets(client, cfg["targets"])
    if not chats:
        print("[!] No groups matched your config. Check the [groups] targets setting.")
        await client.disconnect()
        return
    print(f"[+] Scanning {len(chats)} group(s) for videos...\n")
    grand = 0
    for entity, gid, title in chats:
        count = 0
        async for msg in _iter_videos(client, entity):
            idx.upsert(video_record(msg, gid, title, cfg["mode"],
                                    cfg["compare_unnamed_by_size"]))
            count += 1
            if count % 100 == 0:
                print(f"    {title}: {count} videos indexed...")
        grand += count
        print(f"[+] {title}: {count} videos.")
    s = idx.stats()
    print(f"\n[+] Done. Index now holds {s['kept']} videos across {s['groups']} group(s).")
    dups = idx.duplicate_groups(cfg["mode"])
    extra = sum(len(g) - 1 for g in dups)
    print(f"[+] Found {len(dups)} duplicate set(s) -> {extra} removable copy(ies).")
    print("    Run:  python tgdedup.py dedup   to review and delete them.")
    idx.close()
    await client.disconnect()


def _ask(prompt, auto=False):
    if auto:
        print(prompt + " y (auto)")
        return True
    while True:
        a = input(prompt + " [y/n/q] ").strip().lower()
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        if a in ("q", "quit"):
            raise KeyboardInterrupt


async def cmd_dedup(cfg, auto=False):
    client = make_client(cfg)
    await client.start()
    idx = Index()
    dups = idx.duplicate_groups(cfg["mode"])
    if not dups:
        print("[+] No duplicates in the index. Run 'scan' first if you haven't.")
        idx.close(); await client.disconnect(); return

    total_removable = sum(len(g) - 1 for g in dups)
    print(f"[+] {len(dups)} duplicate set(s), {total_removable} copy(ies) can be removed.")
    print("    Keeping the OLDEST copy of each; you confirm every deletion.\n")

    removed = 0
    try:
        for gi, group in enumerate(dups, 1):
            keep = group[0]                      # oldest (sorted asc by date)
            victims = group[1:]
            print(f"── Set {gi}/{len(dups)}: \"{keep['filename'] or '(no name)'}\" "
                  f"({human_size(keep['size'])}) — {len(group)} copies")
            print(f"     KEEP  : {keep['group_name']}  msg {keep['message_id']}  "
                  f"{keep['date']}")
            for v in victims:
                print(f"     delete: {v['group_name']}  msg {v['message_id']}  {v['date']}")
            if not _ask(f"   Delete {len(victims)} newer copy(ies)?", auto):
                print("     skipped.\n")
                continue
            for v in victims:
                try:
                    ent = await client.get_entity(v["group_id"])
                    await client.delete_messages(ent, [v["message_id"]], revoke=True)
                    idx.mark_deleted(v["group_id"], v["message_id"])
                    removed += 1
                    print(f"     ✓ deleted msg {v['message_id']} in {v['group_name']}")
                except Exception as e:
                    print(f"     ✗ could not delete msg {v['message_id']}: {e}")
            print()
    except KeyboardInterrupt:
        print("\n[+] Stopped by user.")
    print(f"[+] Finished. {removed} duplicate video(s) deleted.")
    idx.close()
    await client.disconnect()


async def catch_up(client, idx, chats, cfg, log=print):
    """
    After a shutdown/reboot, index any videos posted while we were offline and
    return the ones that duplicate an already-kept video. Only fetches messages
    newer than what we've already seen (fast).
    """
    found = []
    for entity, gid, title in chats:
        watermark = idx.max_message_id(gid)
        if watermark == 0:
            continue  # never scanned this group; a full 'scan' should run first
        new_msgs = []
        for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
            try:
                async for msg in client.iter_messages(entity, min_id=watermark, filter=flt):
                    if is_video(msg) and not idx.has_message(gid, msg.id):
                        new_msgs.append(msg)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
        for msg in sorted(new_msgs, key=lambda m: m.id):
            rec = video_record(msg, gid, title, cfg["mode"], cfg["compare_unnamed_by_size"])
            matches = idx.find_matches(rec["norm_name"], rec["size"], cfg["mode"])
            if matches:
                found.append((rec, matches[0], entity))
            else:
                idx.upsert(rec)
    if found:
        log(f"[+] Catch-up: {len(found)} duplicate video(s) posted while offline.")
    return found


async def cmd_watch(cfg, auto=False):
    client = make_client(cfg)
    await client.start()
    idx = Index()
    chats = await resolve_targets(client, cfg["targets"])
    watch_ids = {gid for _, gid, _ in chats}
    titles = {gid: title for _, gid, title in chats}

    # ---- catch up on anything posted while the tool was closed / PC was off ----
    print("[+] Checking for videos added while the tool was off...")
    pending = await catch_up(client, idx, chats, cfg)
    for rec, oldest, entity in pending:
        print(f"\n[!] (offline dup) \"{rec['filename']}\" ({human_size(rec['size'])}) "
              f"in {rec['group_name']} — already exists in {oldest['group_name']}")
        if _ask("    Delete this duplicate?", auto):
            try:
                await client.delete_messages(entity, [rec["message_id"]], revoke=True)
                rec["status"] = "deleted"; idx.upsert(rec)
                print("    ✓ deleted.")
            except Exception as e:
                print(f"    ✗ {e}")
        else:
            idx.upsert(rec)

    print(f"\n[+] Watching {len(watch_ids)} group(s) for new videos. Press Ctrl+C to stop.\n")

    loop = asyncio.get_event_loop()

    @client.on(events.NewMessage(chats=[e for e, _, _ in chats]))
    async def handler(event):
        msg = event.message
        if not is_video(msg):
            return
        gid = event.chat_id
        title = titles.get(gid, str(gid))
        rec = video_record(msg, gid, title, cfg["mode"], cfg["compare_unnamed_by_size"])
        matches = idx.find_matches(rec["norm_name"], rec["size"], cfg["mode"])
        if not matches:
            idx.upsert(rec)
            print(f"[+] NEW unique video kept: \"{rec['filename']}\" "
                  f"({human_size(rec['size'])}) in {title}")
            return
        # Duplicate of something already kept.
        oldest = matches[0]
        print(f"\n[!] DUPLICATE posted in {title}: \"{rec['filename']}\" "
              f"({human_size(rec['size'])})")
        print(f"    already exists in {oldest['group_name']} (msg {oldest['message_id']}, "
              f"{oldest['date']})")
        do_delete = auto
        if not auto:
            do_delete = await loop.run_in_executor(
                None, lambda: input("    Delete this new duplicate? [y/n] ")
                                  .strip().lower() in ("y", "yes"))
        else:
            print("    -> auto-deleting")
        if do_delete:
            try:
                await client.delete_messages(await event.get_chat(), [msg.id], revoke=True)
                rec["status"] = "deleted"
                idx.upsert(rec)
                idx.mark_deleted(gid, msg.id)
                print("    ✓ deleted.\n")
            except Exception as e:
                print(f"    ✗ could not delete: {e}\n")
        else:
            idx.upsert(rec)   # keep record so we don't re-prompt endlessly
            print("    kept.\n")

    await client.run_until_disconnected()


async def cmd_copy(cfg, source, dest):
    """Copy all videos from one group to another, then index the destination."""
    if not source or not dest:
        print("[!] Use: python tgdedup.py copy --source <group> --dest <group>"); return
    client = make_client(cfg)
    await client.start()
    idx = Index()
    src_ent = await client.get_entity(int(source) if source.lstrip("-").isdigit() else source)
    dst_ent = await client.get_entity(int(dest) if dest.lstrip("-").isdigit() else dest)
    print(f"[+] Collecting videos in source...")
    ids = []
    async for msg in _iter_videos(client, src_ent):
        ids.append(msg.id)
    ids.sort()
    if not ids:
        print("[!] No videos in source."); await client.disconnect(); return
    print(f"[+] Copying {len(ids)} video(s)...")
    done = 0
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        while True:
            try:
                await client.forward_messages(dst_ent, batch, src_ent); break
            except FloodWaitError as e:
                print(f"    (pacing {e.seconds}s)"); await asyncio.sleep(e.seconds + 1)
        done += len(batch)
        print(f"    {done}/{len(ids)}")
        await asyncio.sleep(1.5)
    print(f"[+] Copied {done} video(s). Run 'scan' + 'dedup' to remove duplicates in the destination.")
    idx.close()
    await client.disconnect()


async def cmd_stats(cfg):
    idx = Index()
    s = idx.stats()
    print(f"Videos indexed (kept): {s['kept']}")
    print(f"Deleted so far       : {s['deleted']}")
    print(f"Groups covered       : {s['groups']}")
    dups = idx.duplicate_groups(cfg["mode"])
    print(f"Duplicate sets pending: {len(dups)} "
          f"({sum(len(g)-1 for g in dups)} removable copies)")
    idx.close()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description="Telegram video duplicate remover")
    p.add_argument("command",
                   choices=["login", "groups", "scan", "dedup", "watch", "stats", "copy"])
    p.add_argument("--config", default="config.ini")
    p.add_argument("--auto", action="store_true",
                   help="delete without asking (use with care)")
    p.add_argument("--source", help="copy: source group (@name or id)")
    p.add_argument("--dest", help="copy: destination group (@name or id)")
    args = p.parse_args()
    cfg = load_config(args.config)

    runners = {
        "login":  lambda: cmd_login(cfg),
        "groups": lambda: cmd_groups(cfg),
        "scan":   lambda: cmd_scan(cfg),
        "dedup":  lambda: cmd_dedup(cfg, args.auto),
        "watch":  lambda: cmd_watch(cfg, args.auto),
        "stats":  lambda: cmd_stats(cfg),
        "copy":   lambda: cmd_copy(cfg, args.source, args.dest),
    }
    try:
        asyncio.run(runners[args.command]())
    except KeyboardInterrupt:
        print("\n[+] Bye.")


if __name__ == "__main__":
    main()
