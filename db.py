"""
SQLite index for the Telegram video duplicate remover.

One row per video message we have seen. The index lets us:
  * remember everything already scanned (Phase 1)
  * instantly tell whether a newly-posted video already exists (Phase 2)
"""

import sqlite3
import os
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       INTEGER NOT NULL,
    group_name     TEXT,
    message_id     INTEGER NOT NULL,
    filename       TEXT,
    norm_name      TEXT,          -- lower-cased / trimmed name used for matching
    size           INTEGER,       -- bytes
    mime_type      TEXT,
    file_unique_id TEXT,          -- Telegram's per-file id (stable copy detector)
    date           TEXT,          -- ISO-8601 of the original message (UTC)
    content_hash   TEXT,          -- SHA-256, filled in only for hash verification
    status         TEXT DEFAULT 'kept',   -- kept | deleted
    indexed_at     TEXT,
    UNIQUE(group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_match  ON videos(norm_name, size);
CREATE INDEX IF NOT EXISTS idx_status ON videos(status);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Index:
    def __init__(self, path="index.db"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- writing -------------------------------------------------------

    def upsert(self, video: dict):
        """Insert (or update) one video row. `video` keys match the columns."""
        cols = ("group_id", "group_name", "message_id", "filename", "norm_name",
                "size", "mime_type", "file_unique_id", "date", "content_hash",
                "status")
        row = {c: video.get(c) for c in cols}
        if row["status"] is None:
            row["status"] = "kept"
        self.conn.execute(
            """
            INSERT INTO videos
                (group_id, group_name, message_id, filename, norm_name, size,
                 mime_type, file_unique_id, date, content_hash, status, indexed_at)
            VALUES
                (:group_id, :group_name, :message_id, :filename, :norm_name, :size,
                 :mime_type, :file_unique_id, :date, :content_hash, :status, :indexed_at)
            ON CONFLICT(group_id, message_id) DO UPDATE SET
                filename       = excluded.filename,
                norm_name      = excluded.norm_name,
                size           = excluded.size,
                mime_type      = excluded.mime_type,
                file_unique_id = excluded.file_unique_id,
                date           = excluded.date
            """,
            {**row, "indexed_at": _now()},
        )
        self.conn.commit()

    def mark_deleted(self, group_id: int, message_id: int):
        self.conn.execute(
            "UPDATE videos SET status='deleted' WHERE group_id=? AND message_id=?",
            (group_id, message_id),
        )
        self.conn.commit()

    def set_hash(self, row_id: int, content_hash: str):
        self.conn.execute("UPDATE videos SET content_hash=? WHERE id=?",
                          (content_hash, row_id))
        self.conn.commit()

    # ---- reading -------------------------------------------------------

    def has_message(self, group_id: int, message_id: int) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM videos WHERE group_id=? AND message_id=?",
            (group_id, message_id))
        return cur.fetchone() is not None

    def max_message_id(self, group_id: int) -> int:
        """Highest message id we have indexed for a group (0 if none).
        Used as a watermark so 'catch-up' only fetches messages posted since."""
        cur = self.conn.execute(
            "SELECT MAX(message_id) FROM videos WHERE group_id=?", (group_id,))
        r = cur.fetchone()[0]
        return r or 0

    def clear_group(self, group_id: int):
        """Drop all rows for a group so a fresh scan reflects Telegram exactly
        (prevents stale/'cached' entries lingering in the index)."""
        self.conn.execute("DELETE FROM videos WHERE group_id=?", (group_id,))
        self.conn.commit()

    def find_matches(self, group_id, norm_name, size, mode="name_size"):
        """Existing KEPT rows in the SAME group that match by the given mode,
        oldest first. Duplicate detection is per-group: a video is a duplicate
        only of other copies inside the same group."""
        if mode == "name":
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE status='kept' AND group_id=? AND norm_name=? "
                "ORDER BY date ASC", (group_id, norm_name))
        else:
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE status='kept' AND group_id=? AND norm_name=? AND size=? "
                "ORDER BY date ASC", (group_id, norm_name, size))
        return [dict(r) for r in cur.fetchall()]

    def duplicate_groups(self, mode="name_size", group_ids=None):
        """
        Return sets of KEPT videos that are duplicates of each other WITHIN THE
        SAME GROUP. Each element is a list of rows sorted oldest-first; the first
        item is the one we keep, the rest are deletion candidates. Duplicates are
        never matched across different groups - so copying group X into group Z
        and then de-duping Z leaves the source group X untouched.

        If group_ids is given, only those groups are considered (so "check group
        Z" shows Z's duplicates only, not other groups still in the index).
        """
        if mode == "name":
            having_key = "group_id || '|' || norm_name"
        else:
            having_key = "group_id || '|' || norm_name || '|' || size"
        gfilter, gparams = "", []
        if group_ids:
            gfilter = " AND group_id IN (%s)" % ",".join("?" for _ in group_ids)
            gparams = list(group_ids)
        cur = self.conn.execute(
            f"""
            SELECT * FROM videos
            WHERE status='kept' AND norm_name IS NOT NULL AND norm_name <> '' {gfilter}
              AND {having_key} IN (
                  SELECT {having_key} FROM videos
                  WHERE status='kept' AND norm_name IS NOT NULL AND norm_name <> '' {gfilter}
                  GROUP BY {having_key} HAVING COUNT(*) > 1
              )
            ORDER BY group_id, norm_name, size, date ASC
            """,
            gparams + gparams,
        )
        groups, current, ck = [], [], None
        for r in cur.fetchall():
            r = dict(r)
            k = ((r["group_id"], r["norm_name"]) if mode == "name"
                 else (r["group_id"], r["norm_name"], r["size"]))
            if k != ck:
                if current:
                    groups.append(current)
                current, ck = [], k
            current.append(r)
        if current:
            groups.append(current)
        return groups

    def cross_group_duplicates(self, variable_gid, static_gids, mode="name_size"):
        """
        Find videos in the VARIABLE group that also exist in any of the STATIC
        groups. These are candidates to delete FROM the variable group only
        (the static groups are never touched).

        Returns pseudo duplicate-sets shaped like duplicate_groups(): each set is
        [keep_row, victim_row] where keep_row describes the static group that
        already has the file, and victim_row is the copy in the variable group.
        """
        static_gids = [g for g in (static_gids or []) if g != variable_gid]
        if not static_gids:
            return []
        place = ",".join("?" for _ in static_gids)
        size_cond = "" if mode == "name" else " AND b.size = a.size"
        cur = self.conn.execute(
            f"""
            SELECT a.*, (
                SELECT b.group_name FROM videos b
                WHERE b.group_id IN ({place}) AND b.status='kept'
                  AND b.norm_name = a.norm_name {size_cond}
                ORDER BY b.date ASC LIMIT 1
            ) AS match_group
            FROM videos a
            WHERE a.group_id = ? AND a.status='kept'
              AND a.norm_name IS NOT NULL AND a.norm_name <> ''
              AND EXISTS (
                  SELECT 1 FROM videos b
                  WHERE b.group_id IN ({place}) AND b.status='kept'
                    AND b.norm_name = a.norm_name {size_cond}
              )
            ORDER BY a.norm_name, a.date ASC
            """,
            static_gids + [variable_gid] + static_gids,
        )
        groups = []
        for r in cur.fetchall():
            a = dict(r)
            match = a.pop("match_group", None) or "another group"
            keep = {
                "group_name": f"exists in: {match}", "date": "kept (not touched)",
                "filename": a["filename"], "size": a["size"],
                # use the variable copy's ids for the preview - it's the same file
                "group_id": a["group_id"], "message_id": a["message_id"],
                "norm_name": a["norm_name"],
            }
            groups.append([keep, a])
        return groups

    def stats(self):
        c = self.conn.execute
        total   = c("SELECT COUNT(*) FROM videos WHERE status='kept'").fetchone()[0]
        deleted = c("SELECT COUNT(*) FROM videos WHERE status='deleted'").fetchone()[0]
        groups  = c("SELECT COUNT(DISTINCT group_id) FROM videos").fetchone()[0]
        return {"kept": total, "deleted": deleted, "groups": groups}

    def close(self):
        self.conn.close()
