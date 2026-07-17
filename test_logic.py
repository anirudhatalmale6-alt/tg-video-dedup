"""Offline test of the PER-GROUP duplicate-detection logic (no Telegram needed)."""
import os
from datetime import datetime, timezone, timedelta
from db import Index

TESTDB = "test_index.db"
if os.path.exists(TESTDB):
    os.remove(TESTDB)

idx = Index(TESTDB)
base = datetime(2024, 1, 1, tzinfo=timezone.utc)
mid = [0]

def add(name, size, days, group, gname):
    mid[0] += 1
    idx.upsert({
        "group_id": group, "group_name": gname, "message_id": mid[0],
        "filename": name, "norm_name": name.strip().lower(), "size": size,
        "mime_type": "video/mp4", "file_unique_id": str(mid[0]),
        "date": (base + timedelta(days=days)).isoformat(),
        "content_hash": None, "status": "kept",
    })

# ---- Scenario 1: within-group duplicates ----
add("movie.mp4", 1000, 0, 1, "GroupA")   # oldest -> keep
add("movie.mp4", 1000, 5, 1, "GroupA")   # dup in same group -> removable
add("clip.mp4", 500, 0, 1, "GroupA")     # unique

groups = idx.duplicate_groups("name_size")
assert len(groups) == 1, f"expected 1 within-group dup set, got {len(groups)}"
assert [r["message_id"] for r in groups[0]] == [1, 2]
print("1. within-group duplicate detected, keep oldest  OK")

# ---- Scenario 2: SAME video in two DIFFERENT groups must NOT be a duplicate ----
add("movie.mp4", 1000, 1, 2, "GroupB")   # same file, different group
g2 = idx.duplicate_groups("name_size")
# still only the GroupA set; GroupB's copy is NOT matched across groups
assert len(g2) == 1 and all(r["group_id"] == 1 for r in g2[0]), \
    f"cross-group must not match: {[(r['group_id'],r['message_id']) for gg in g2 for r in gg]}"
print("2. same video in different groups is NOT flagged (per-group)  OK")

# find_matches is per-group
assert len(idx.find_matches(1, "movie.mp4", 1000, "name_size")) == 2   # two in GroupA
assert len(idx.find_matches(2, "movie.mp4", 1000, "name_size")) == 1   # one in GroupB
assert idx.find_matches(3, "movie.mp4", 1000, "name_size") == []       # none in GroupC
print("3. find_matches scoped to the group  OK")

# ---- Scenario 3: client's copy case - copy X(A,B,C) into Z twice ----
idx2 = Index("test_index2.db") if not os.path.exists("test_index2.db") else None
for db in ("test_index2.db",):
    if os.path.exists(db): os.remove(db)
z = Index("test_index2.db")
mid[0] = 0
def addz(name, size, days, group, gname):
    mid[0]+=1
    z.upsert({"group_id":group,"group_name":gname,"message_id":mid[0],"filename":name,
              "norm_name":name.lower(),"size":size,"mime_type":"video/mp4",
              "file_unique_id":str(mid[0]),"date":(base+timedelta(days=days)).isoformat(),
              "content_hash":None,"status":"kept"})
# source X (group 1): A,B,C once each
for i,n in enumerate(["a.mp4","b.mp4","c.mp4"]):
    addz(n, 100+i, 0, 1, "X")
# dest Z (group 2): copied twice -> A,B,C each appear twice (day 10 and day 20)
for day in (10, 20):
    for i,n in enumerate(["a.mp4","b.mp4","c.mp4"]):
        addz(n, 100+i, day, 2, "Z")

zdups = z.duplicate_groups("name_size")
assert len(zdups) == 3, f"expected 3 dup sets in Z, got {len(zdups)}"
# every set is inside Z (group 2), keeps the OLDER Z copy, removes the newer
removable = []
for g in zdups:
    assert all(r["group_id"] == 2 for r in g), "must be within Z only - source X untouched"
    assert g[0]["date"] < g[1]["date"]      # keep oldest
    removable += g[1:]
assert len(removable) == 3, f"expected 3 removable copies in Z, got {len(removable)}"
assert all(r["group_id"] == 2 for r in removable), "never delete from source group X"
print("4. copy X->Z twice: 3 dups found IN Z, source X kept, deletes only newer Z copies  OK")

# ---- clear_group wipes stale rows ----
z.clear_group(2)
assert z.duplicate_groups("name_size") == []
print("5. clear_group removes a group's rows (fresh scan, no caching)  OK")

idx.close(); z.close()
for db in (TESTDB, "test_index2.db"):
    if os.path.exists(db): os.remove(db)
print("\nALL TESTS PASSED")
