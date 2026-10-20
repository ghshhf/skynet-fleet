"""L3 共享上下文（TinyContext 式）：跨设备同一份记忆，逐条可删(E-7)。

Commander（中枢/路由=海马体）持有此库；节点经协议读写。
纯 stdlib + sqlite3；优先用 FTS5（中文走 LIKE 兜底），磁盘持久化。
"""
import os
import sqlite3
import threading
import time


def fts5_available():
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(ns, key, val)")
        con.close()
        return True
    except sqlite3.Error:
        return False


_HAS_FTS5 = fts5_available()


def _has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


class SharedContext:
    def __init__(self, path="fleet_context.db"):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False)
        self._lk = threading.Lock()
        with self._lk:
            self.con.execute("""CREATE TABLE IF NOT EXISTS kv(
                ns TEXT, key TEXT, val TEXT, ts REAL, PRIMARY KEY(ns,key))""")
            if _HAS_FTS5:
                try:
                    self.con.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS kv_fts USING fts5(ns, key, val)")
                except sqlite3.Error:
                    pass
            self.con.commit()

    def put(self, ns, key, val):
        ts = time.time()
        with self._lk:
            self.con.execute(
                "INSERT INTO kv(ns,key,val,ts) VALUES(?,?,?,?) "
                "ON CONFLICT(ns,key) DO UPDATE SET val=excluded.val, ts=excluded.ts",
                (ns, key, val, ts))
            if _HAS_FTS5:
                self.con.execute("DELETE FROM kv_fts WHERE key=?", (key,))
                self.con.execute("INSERT INTO kv_fts(ns,key,val) VALUES(?,?,?)", (ns, key, val))
            self.con.commit()
        return {"ok": True, "ts": ts}

    def get(self, ns, key):
        with self._lk:
            row = self.con.execute(
                "SELECT val,ts FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        if not row:
            return {"ok": False, "err": "not found"}
        return {"ok": True, "val": row[0], "ts": row[1]}

    def search(self, ns, query, k=10):
        with self._lk:
            if _HAS_FTS5 and not _has_cjk(query):
                rows = self.con.execute(
                    "SELECT ns,key,val,ts FROM kv_fts WHERE kv_fts MATCH ?", (query,)
                ).fetchall()
            else:
                like = f"%{query}%"
                rows = self.con.execute(
                    "SELECT ns,key,val,ts FROM kv WHERE (key LIKE ? OR val LIKE ?) ORDER BY ts DESC",
                    (like, like)).fetchall()
        out = [{"key": r[1], "val": r[2], "ts": r[3]}
               for r in rows if r[0] == ns][:k]
        return out

    def delete(self, ns, key):
        """E-7：逐条可删。"""
        with self._lk:
            self.con.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))
            if _HAS_FTS5:
                self.con.execute("DELETE FROM kv_fts WHERE key=?", (key,))
            self.con.commit()
        return {"ok": True}

    def recent(self, ns, k=20):
        with self._lk:
            rows = self.con.execute(
                "SELECT key,val,ts FROM kv WHERE ns=? ORDER BY ts DESC LIMIT ?", (ns, k)).fetchall()
        return [{"key": r[0], "val": r[1], "ts": r[2]} for r in rows]

    def close(self):
        with self._lk:
            self.con.close()


if __name__ == "__main__":
    c = SharedContext(":memory:")
    c.put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关")
    print("get:", c.get("her", "game_lastnight"))
    print("search 昨晚:", c.search("her", "昨晚"))
    c.delete("her", "game_lastnight")
    print("after del:", c.get("her", "game_lastnight"))
