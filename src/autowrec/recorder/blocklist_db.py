import logging
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

# blocklist_db is imported very early (before console's Rich setup may be ready),
# so we use the stdlib logger directly rather than importing console helpers.
_log = logging.getLogger("autowrec.blocklist")


class LRUCache:
    """Simple LRU cache backed by an OrderedDict."""

    __slots__ = ("_capacity", "_data")

    def __init__(self, capacity: int = 4096):
        self._capacity = capacity
        self._data: OrderedDict[str, bool] = OrderedDict()

    def get(self, key: str) -> bool | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: bool) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        else:
            if len(self._data) >= self._capacity:
                self._data.popitem(last=False)
        self._data[key] = value

    def clear(self) -> None:
        self._data.clear()


def _reverse_domain(domain: str) -> str:
    """Reverse a domain for prefix-based subdomain matching.

    Example: 'ads.google.com' -> 'com.google.ads'

    This lets us use SQLite's `LIKE 'com.google.%'` or `BETWEEN` to match
    all subdomains of google.com in a single indexed query.
    """
    return ".".join(reversed([p for p in domain.lower().strip(".").split(".") if p]))


def _extract_domain(url: str) -> str:
    """Pull the hostname out of a URL, lowercased."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception as exc:
        _log.debug("Failed to parse URL %r: %s", url, exc)
        return ""


class BlocklistDB:
    """SQLite-backed domain blocklist with reversed-domain indexing and LRU cache.

    Features:
        - Subdomain matching via reversed-domain prefix queries
        - Multiple named lists with independent enable/disable
        - LRU cache so hot domains skip the DB entirely
        - Atomic bulk inserts for fast list loading

    Schema:
        domains(reversed_domain TEXT, source TEXT, added_at REAL)
        sources(name TEXT PK, url TEXT, enabled INT, loaded_at REAL, domain_count INT)
    """

    def __init__(self, db_path: str = ":memory:", cache_size: int = 4096):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._cache = LRUCache(cache_size)
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        except sqlite3.Error as exc:
            _log.error("Failed to open/initialise blocklist DB at %s: %s", db_path, exc, exc_info=True)
            raise

    def _init_schema(self) -> None:
        try:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    name        TEXT PRIMARY KEY,
                    url         TEXT,
                    enabled     INTEGER DEFAULT 1,
                    loaded_at   REAL,
                    domain_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS domains (
                    reversed_domain TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    added_at        REAL NOT NULL,
                    FOREIGN KEY (source) REFERENCES sources(name)
                );

                CREATE INDEX IF NOT EXISTS idx_domains_reversed
                    ON domains(reversed_domain);

                CREATE INDEX IF NOT EXISTS idx_domains_source
                    ON domains(source);
            """)
            self._conn.commit()
        except sqlite3.Error as exc:
            _log.error("Failed to create blocklist schema: %s", exc, exc_info=True)
            raise

    # ── List management ──────────────────────────────────────────────

    def load_file(self, path: str, source_name: str | None = None, source_url: str | None = None) -> int:
        """Parse a hosts/domain-list file and bulk-insert into the DB.

        Supports:
            - Plain domain lists  (one domain per line)
            - Hosts format        (0.0.0.0 domain.com  /  127.0.0.1 domain.com)
            - Comment lines       (# or !)

        Returns the number of domains inserted.
        """
        source_name = source_name or Path(path).stem
        now = time.time()
        domains = []

        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.error("Cannot open blocklist file %s: %s", path, exc)
            return 0

        with fh as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in ("#", "!", "/"):
                    continue

                parts = line.split()

                if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
                    domain = parts[1].lower()
                elif len(parts) == 1:
                    domain = parts[0].lower()
                else:
                    continue

                if "." not in domain or domain in ("0.0.0.0", "localhost", "localhost.localdomain"):
                    continue

                domains.append((_reverse_domain(domain), source_name, now))

        if not domains:
            return 0

        try:
            cur = self._conn.cursor()

            cur.execute("DELETE FROM domains WHERE source = ?", (source_name,))

            cur.executemany("INSERT INTO domains (reversed_domain, source, added_at) VALUES (?, ?, ?)", domains)

            cur.execute(
                """
                INSERT INTO sources (name, url, enabled, loaded_at, domain_count)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    url = excluded.url,
                    loaded_at = excluded.loaded_at,
                    domain_count = excluded.domain_count
            """,
                (source_name, source_url or path, now, len(domains)),
            )

            self._conn.commit()
            self._cache.clear()
            return len(domains)
        except sqlite3.Error as exc:
            _log.error("Failed to load blocklist source '%s': %s", source_name, exc, exc_info=True)
            return 0

    def list_sources(self) -> list[dict]:
        """Return metadata for all loaded sources."""
        cur = self._conn.execute("SELECT name, url, enabled, loaded_at, domain_count FROM sources ORDER BY name")
        return [
            {"name": r[0], "url": r[1], "enabled": bool(r[2]), "loaded_at": r[3], "domain_count": r[4]}
            for r in cur.fetchall()
        ]

    # ── Lookup ───────────────────────────────────────────────────────

    def is_blocked(self, hostname: str) -> bool:
        """Check whether a hostname (or any of its parent domains) is blocked.

        Walks up the domain hierarchy:
            ads.tracker.example.com
                tracker.example.com
                        example.com

        Uses the LRU cache first, then falls back to a single SQL query
        that checks all levels at once.
        """
        hostname = hostname.lower().strip(".")
        if not hostname:
            return False

        cached = self._cache.get(hostname)
        if cached is not None:
            return cached

        # Build all domain levels to check
        # e.g. "a.b.c.com" -> ["com.c.b.a", "com.c.b", "com.c", "com"]
        parts = hostname.split(".")
        reversed_candidates = []
        for i in range(len(parts)):
            # Take from index i to end, reverse
            sub = ".".join(parts[i:])
            reversed_candidates.append(_reverse_domain(sub))

        placeholders = ",".join("?" * len(reversed_candidates))
        try:
            cur = self._conn.execute(
                f"""
                SELECT 1 FROM domains d
                JOIN sources s ON d.source = s.name
                WHERE s.enabled = 1
                  AND d.reversed_domain IN ({placeholders})
                LIMIT 1
            """,
                reversed_candidates,
            )

            blocked = cur.fetchone() is not None
        except sqlite3.Error as exc:
            _log.warning("Blocklist lookup failed for %s: %s", hostname, exc)
            return False

        self._cache.put(hostname, blocked)
        return blocked

    def is_blocked_url(self, url: str) -> bool:
        """Convenience: extract hostname from a URL and check it."""
        domain = _extract_domain(url)
        if not domain:
            return False
        return self.is_blocked(domain)

    # ── Stats ────────────────────────────────────────────────────────

    def total_domains(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM domains")
        return cur.fetchone()[0]

    def total_enabled_domains(self) -> int:
        cur = self._conn.execute("""
            SELECT COUNT(*) FROM domains d
            JOIN sources s ON d.source = s.name
            WHERE s.enabled = 1
        """)
        return cur.fetchone()[0]

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        self._cache.clear()
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                _log.warning("Error closing blocklist DB: %s", exc)
            finally:
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self) -> str:
        sources = len(self.list_sources())
        total = self.total_domains()
        enabled = self.total_enabled_domains()
        return f"<BlocklistDB sources={sources} domains={total} enabled={enabled}>"
