"""Search past chats: a SQLite FTS5 index (stdlib sqlite3, no model) over the saved chats in outputs/chats.

The index is in outputs/memory/chats.sqlite3. It holds each chat's title and the text of its user and assistant
messages (not the reasoning and not the tool results). Before each search, sync() reads only the chats whose
chat.json changed since the last search (size and mtime), drops the deleted ones and skips temporary chats
("temporary": true in chat.json). A search never returns the chat it runs in.

Matching folds case and accents the same way in the index and in the query (fold()): "İstanbul", "istanbul" and
"ISTANBUL" match, and so do "şikayet" and "sikayet". Each query word also matches the words that start with it, so
"kedi" finds "kedimi" (Turkish adds suffixes to words).
"""

import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import ROOT

DB = ROOT / "outputs" / "memory" / "chats.sqlite3"
SCHEMA = 1  # a different value in the file rebuilds the index
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")  # the chat id format of app/chat.py
MAX_TERMS = 8  # query words used
MAX_CHATS = 10
SNIPPET = 160  # characters of one snippet
SNIPPETS = 2  # snippets of one chat
# Turkish: dotted capital I and dotless small i fold to "i", as the plain I does
TURKISH = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
lock = threading.Lock()  # one sync or search at a time


def chats_folder() -> Path:
    from app import (
        chat,
    )  # imported here: app.chat loads the providers, which load this module

    return chat.CHATS


def fold_char(c: str) -> str:
    c = c.translate(TURKISH)
    c = "".join(
        x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x)
    )
    return c.casefold()


def fold(text: str) -> str:
    """The text in lower case, without accents, with the Turkish i forms as "i" (for the index and the query)."""
    text = unicodedata.normalize("NFKD", text.translate(TURKISH))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def available() -> bool:
    """Whether this Python's SQLite has FTS5."""
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        return True
    except sqlite3.Error:
        return False


def connect() -> sqlite3.Connection:
    """The index, made or rebuilt when it is missing or has another SCHEMA."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA:
        db.executescript(
            "DROP TABLE IF EXISTS chats; DROP TABLE IF EXISTS msgs;"
            "CREATE TABLE chats (id TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, title TEXT, date TEXT,"
            " skipped INTEGER);"
            "CREATE VIRTUAL TABLE msgs USING fts5(body, chat UNINDEXED, role UNINDEXED, original UNINDEXED,"
            " tokenize = 'unicode61 remove_diacritics 2');"
            f"PRAGMA user_version = {SCHEMA};"
        )
    return db


def messages_of(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The (role, text) rows of a chat to index: the title, then each user and assistant text."""
    rows = [("title", str(data.get("title") or ""))]
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        if m.get("app"):  # a note of the app, not the user's words
            continue
        text = m.get("content")
        if isinstance(text, str) and text.strip():
            rows.append((m["role"], text))
    return rows


def index_chat(db: sqlite3.Connection, cid: str, path: Path, st: Any) -> None:
    db.execute("DELETE FROM msgs WHERE chat = ?", (cid,))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    skipped = not isinstance(data, dict) or data.get("temporary") is True
    title = date = ""
    if not skipped:
        title = str(data.get("title") or "")
        date = str(data.get("updated") or data.get("created") or "")[:10]
        db.executemany(
            "INSERT INTO msgs (body, chat, role, original) VALUES (?, ?, ?, ?)",
            [(fold(t), cid, role, t) for role, t in messages_of(data) if t],
        )
    db.execute(
        "INSERT OR REPLACE INTO chats (id, size, mtime, title, date, skipped) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, st.st_size, st.st_mtime_ns, title, date, int(skipped)),
    )


def sync(db: sqlite3.Connection) -> int:
    """Bring the index up to date with outputs/chats; returns how many chats it read again."""
    known = {
        r[0]: (r[1], r[2]) for r in db.execute("SELECT id, size, mtime FROM chats")
    }
    found: set[str] = set()
    changed = 0
    root = chats_folder()
    for entry in root.iterdir() if root.is_dir() else []:
        if not ID.fullmatch(
            entry.name
        ):  # also skips the hidden trash names of deleted chats
            continue
        path = entry / "chat.json"
        try:
            st = path.stat()
        except OSError:
            continue
        found.add(entry.name)
        if known.get(entry.name) != (st.st_size, st.st_mtime_ns):
            index_chat(db, entry.name, path, st)
            changed += 1
    for cid in set(known) - found:
        db.execute("DELETE FROM msgs WHERE chat = ?", (cid,))
        db.execute("DELETE FROM chats WHERE id = ?", (cid,))
    db.commit()
    return changed


def rebuild() -> dict[str, int]:
    """Delete the index and build it again from outputs/chats."""
    with lock:
        DB.unlink(missing_ok=True)
        with closing(connect()) as db:
            sync(db)
            return counts(db)


def counts(db: sqlite3.Connection) -> dict[str, int]:
    chats = db.execute("SELECT COUNT(*) FROM chats WHERE skipped = 0").fetchone()[0]
    msgs = db.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
    return {"chats": chats, "messages": msgs}


def status() -> dict[str, Any]:
    """For the Settings screen: whether FTS5 is there, and the size of the index (after a sync)."""
    if not available():
        return {"available": False, "chats": 0, "messages": 0}
    with lock, closing(connect()) as db:
        sync(db)
        return {"available": True, **counts(db)}


def terms_of(query: str) -> list[str]:
    out: list[str] = []
    for t in re.findall(r"\w+", fold(query)):
        if t not in out:
            out.append(t)
    return out[:MAX_TERMS]


def snippet(text: str, terms: list[str]) -> str:
    """About SNIPPET characters of the original text around the first query word in it, on one line."""
    parts: list[str] = []
    where: list[int] = []  # the index in `text` of each folded character
    for i, c in enumerate(text):
        f = fold_char(c)
        parts.append(f)
        where += [i] * len(f)
    folded = "".join(parts)
    hits = [p for p in (folded.find(t) for t in terms) if p >= 0]
    start = where[min(hits)] if hits and where else 0
    start = max(0, start - SNIPPET // 3)
    piece = " ".join(text[start : start + SNIPPET].split())
    return (
        ("..." if start else "")
        + piece
        + ("..." if start + SNIPPET < len(text) else "")
    )


def search(query: str, exclude: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """The chats that match, best first: [{"id", "title", "date", "snippets": [(role, text)]}]. All query words must
    match; when no chat has them all, any word may."""
    terms = terms_of(query)
    if not terms:
        return []
    limit = max(1, min(limit, MAX_CHATS))
    with lock, closing(connect()) as db:
        sync(db)
        rows: list[tuple[str, str, str]] = []
        for joiner in (" AND ", " OR "):
            match = joiner.join(f'"{t}"*' for t in terms)
            rows = db.execute(
                "SELECT chat, role, original FROM msgs WHERE msgs MATCH ? AND chat != ? ORDER BY bm25(msgs) "
                "LIMIT 200",
                (match, exclude),
            ).fetchall()
            if rows or len(terms) == 1:
                break
        found: dict[str, dict[str, Any]] = {}
        for cid, role, original in rows:
            if cid not in found:
                if len(found) == limit:
                    continue
                title, date = db.execute(
                    "SELECT title, date FROM chats WHERE id = ?", (cid,)
                ).fetchone()
                found[cid] = {"id": cid, "title": title, "date": date, "snippets": []}
            hit = found[cid]
            if role != "title" and len(hit["snippets"]) < SNIPPETS:
                hit["snippets"].append((role, snippet(original, terms)))
        return list(found.values())
