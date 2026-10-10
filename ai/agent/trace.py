"""Agent traces: one row per answer, so engineer mode can show how it answered.

Rows go into the `agent_traces` table of CS 1's SQLite file (schema in PROJECT_PLAN 7.1,
unchanged) when that file and table exist. Otherwise they stay in memory only, which is
what you want on a laptop. Memory also keeps the last 50 so ask.py can print them.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ai.agent import config

_db_path = config.BOX_DB
_recent = {}            # trace_id -> row dict (last 50)
_next_id = 1


def configure(db_path):
    """Point traces at a different SQLite file (tests use a temp file). None = memory only."""
    global _db_path
    _db_path = db_path


class Trace:
    """Collects everything about one answer, then save()s it."""

    def __init__(self, channel, question):
        self.channel, self.question = channel, question
        self.route = None
        self.intent = None
        self.tools = {}         # tool name -> result
        self.chunks = []        # [{"id","title","score"}]
        self.sensors = {}
        self.timings = {}       # step -> milliseconds
        self.notes = []         # e.g. "primary model failed: ..."
        self.model = None
        self.answer = ""
        self.canned_score = None

    @contextmanager
    def step(self, name):
        """with trace.step("rag"): ...   -> timings["rag"] = ms"""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = round((time.perf_counter() - t0) * 1000, 1)

    def to_row(self):
        return {
            "ts": datetime.now(timezone.utc).isoformat(), "channel": self.channel,
            "question": self.question, "route": self.route,
            "tools_json": json.dumps(self.tools, default=str),
            "chunks_json": json.dumps(self.chunks),
            "sensors_json": json.dumps(self.sensors, default=str),
            "answer": self.answer,
            "timings_json": json.dumps({**self.timings, "model": self.model, "notes": self.notes}),
        }

    def save(self, answer, route):
        """Store the row and return its trace_id."""
        global _next_id
        self.answer, self.route = answer, route
        row = self.to_row()
        trace_id = _save_sqlite(row)
        if trace_id is None:                    # no database: use a memory counter
            trace_id, _next_id = _next_id, _next_id + 1
        _recent[trace_id] = {**row, "id": trace_id, "_trace": self}
        for old in sorted(_recent)[:-50]:
            del _recent[old]
        return trace_id


def _save_sqlite(row):
    """Insert into agent_traces. Returns the row id, or None if there is no usable database."""
    if not _db_path or not _exists(_db_path):
        return None
    try:
        con = sqlite3.connect(str(_db_path), timeout=2)
        try:
            cur = con.execute(
                "INSERT INTO agent_traces (ts, channel, question, route, tools_json, chunks_json,"
                " sensors_json, answer, timings_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["channel"], row["question"], row["route"], row["tools_json"],
                 row["chunks_json"], row["sensors_json"], row["answer"], row["timings_json"]))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except sqlite3.Error:                       # table missing, db locked, ...: never break an answer
        return None


def _exists(path):
    import os
    return os.path.exists(str(path))


def get(trace_id):
    """A recent trace row (dict) from memory, or None."""
    return _recent.get(trace_id)


def last():
    return _recent[max(_recent)] if _recent else None
