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

# ---- Scenario 6: UNNAMED videos matched by size+duration key ----
for db in ("test_index3.db",):
    if os.path.exists(db): os.remove(db)
u = Index("test_index3.db")
mid[0] = 0
def addu(normname, size, days, group):
    mid[0]+=1
    u.upsert({"group_id":group,"group_name":"G","message_id":mid[0],"filename":"",
              "norm_name":normname,"size":size,"mime_type":"video/mp4",
              "file_unique_id":str(mid[0]),"date":(base+timedelta(days=days)).isoformat(),
              "content_hash":None,"status":"kept"})
# two unnamed copies of the same video (same size+duration key) in group 2 -> duplicate
addu("__vid__500__30", 500, 0, 2)
addu("__vid__500__30", 500, 3, 2)
# an unnamed video with different duration -> not a duplicate
addu("__vid__500__99", 500, 0, 2)
udups = u.duplicate_groups("name_size")
assert len(udups) == 1 and len(udups[0]) == 2, f"unnamed dup by size+dur failed: {udups}"
print("6. unnamed videos matched by size+duration key  OK")

# ---- Scenario 7: group_ids scoping ----
addu("__vid__700__10", 700, 0, 1)   # group 1
addu("__vid__700__10", 700, 5, 1)   # dup in group 1
all_dups = u.duplicate_groups("name_size")
only_g2 = u.duplicate_groups("name_size", [2])
only_g1 = u.duplicate_groups("name_size", [1])
assert len(all_dups) == 2, f"expected 2 total, got {len(all_dups)}"
assert len(only_g2) == 1 and all(r["group_id"]==2 for r in only_g2[0])
assert len(only_g1) == 1 and all(r["group_id"]==1 for r in only_g1[0])
print("7. duplicate_groups scoped to given group_ids only  OK")

# ---- Scenario 8: cross-group check (delete from variable, keep static) ----
for db in ("test_index4.db",):
    if os.path.exists(db): os.remove(db)
c = Index("test_index4.db")
mid[0] = 0
def addc(name, size, group):
    mid[0]+=1
    c.upsert({"group_id":group,"group_name":f"G{group}","message_id":mid[0],"filename":name,
              "norm_name":name.lower(),"size":size,"mime_type":"video/mp4",
              "file_unique_id":str(mid[0]),"date":base.isoformat(),
              "content_hash":None,"status":"kept"})
# variable group A (id 1): x, y, z
addc("x.mp4",1,1); addc("y.mp4",2,1); addc("z.mp4",3,1)
# static B (id 2) has y ; static C (id 3) has z ; neither has x
addc("y.mp4",2,2)
addc("z.mp4",3,3)
cross = c.cross_group_duplicates(1, [2,3], "name_size")
victims = sorted(g[1]["filename"] for g in cross)
assert victims == ["y.mp4","z.mp4"], victims           # x kept (unique), y & z removable from A
assert all(g[1]["group_id"] == 1 for g in cross)       # victims are always in the variable group
print("8. cross-group: deletes from variable group only, keeps static  OK")
# static groups must never be offered for deletion even if reversed
rev = c.cross_group_duplicates(2, [1], "name_size")
assert [g[1]["filename"] for g in rev] == ["y.mp4"] and rev[0][1]["group_id"] == 2
print("9. cross-group direction is respected (variable is the one emptied)  OK")
c.close()

u.close()
idx.close(); z.close()
for db in (TESTDB, "test_index2.db", "test_index3.db", "test_index4.db"):
    if os.path.exists(db): os.remove(db)
print("\nALL TESTS PASSED")
