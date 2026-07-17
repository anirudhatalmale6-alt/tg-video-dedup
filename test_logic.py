"""Offline test of the duplicate-detection logic (no Telegram needed)."""
import os
from datetime import datetime, timezone, timedelta
from db import Index

TESTDB = "test_index.db"
if os.path.exists(TESTDB):
    os.remove(TESTDB)

idx = Index(TESTDB)
base = datetime(2024, 1, 1, tzinfo=timezone.utc)

def add(mid, name, size, days, group=1, gname="GroupA"):
    idx.upsert({
        "group_id": group, "group_name": gname, "message_id": mid,
        "filename": name, "norm_name": name.strip().lower(), "size": size,
        "mime_type": "video/mp4", "file_unique_id": str(mid),
        "date": (base + timedelta(days=days)).isoformat(),
        "content_hash": None, "status": "kept",
    })

# Two exact duplicates (same name+size), posted on different days.
add(1, "movie.mp4", 1000, 0)    # oldest -> should be KEPT
add(2, "movie.mp4", 1000, 5)    # newer  -> deletable
add(3, "movie.mp4", 1000, 2, group=2, gname="GroupB")  # newer, other group -> deletable

# Same name but DIFFERENT size -> NOT a duplicate under name_size.
add(4, "clip.mp4", 500, 0)
add(5, "clip.mp4", 999, 0)

# Unique file.
add(6, "unique.mp4", 700, 0)

print("== mode = name_size ==")
groups = idx.duplicate_groups("name_size")
assert len(groups) == 1, f"expected 1 dup set, got {len(groups)}"
g = groups[0]
assert [r["message_id"] for r in g] == [1, 3, 2], [r["message_id"] for r in g]
print(f"  duplicate set: keep msg {g[0]['message_id']} (oldest), "
      f"delete {[r['message_id'] for r in g[1:]]}  OK")

# find_matches for a newly arriving movie.mp4 / 1000
m = idx.find_matches("movie.mp4", 1000, "name_size")
assert m and m[0]["message_id"] == 1
print(f"  new movie.mp4/1000 -> matches existing, oldest is msg {m[0]['message_id']}  OK")

# A different-size clip should NOT match.
m2 = idx.find_matches("clip.mp4", 123, "name_size")
assert m2 == []
print("  clip.mp4 with new size -> no match (correct)  OK")

print("\n== mode = name (looser) ==")
groups_n = idx.duplicate_groups("name")
names = sorted(g[0]["norm_name"] for g in groups_n)
assert names == ["clip.mp4", "movie.mp4"], names
print(f"  duplicate sets by name: {names}  OK")

# Deletion bookkeeping.
idx.mark_deleted(1, 2)
idx.mark_deleted(2, 3)
groups_after = idx.duplicate_groups("name_size")
assert groups_after == [], groups_after
print("\n  after deleting the 2 newer copies -> no duplicates remain  OK")

s = idx.stats()
print(f"  stats: {s}")
assert s["deleted"] == 2 and s["kept"] == 4

idx.close()
os.remove(TESTDB)
print("\nALL TESTS PASSED")
