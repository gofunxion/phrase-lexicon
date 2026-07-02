"""SQLite storage for flashcards and review state."""

import os
import sqlite3
from pathlib import Path

from parser import card_identity, classify_hint, normalize_card
from scheduler import initial_card_state, today

_data_dir = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
_data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = _data_dir / "cards.db"

CARDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    group_key TEXT NOT NULL DEFAULT '',
    deck TEXT NOT NULL,
    source TEXT NOT NULL,
    interval INTEGER NOT NULL DEFAULT 0,
    ease REAL NOT NULL DEFAULT 2.5,
    due TEXT NOT NULL,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    UNIQUE(direction, front, back)
)
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_cards_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "cards")
    if not columns:
        conn.execute(CARDS_SCHEMA)
        return

    if "direction" in columns and "front" in columns:
        if "visual_hint" not in columns:
            conn.execute(
                "ALTER TABLE cards ADD COLUMN visual_hint TEXT NOT NULL DEFAULT ''"
            )
        return

    conn.execute("DROP TABLE IF EXISTS cards")
    conn.execute(CARDS_SCHEMA)


def init_db() -> None:
    with connect() as conn:
        _migrate_cards_table(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def get_meta(key: str) -> str | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _load_existing_identities(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    rows = conn.execute("SELECT direction, front, back FROM cards").fetchall()
    return {card_identity({"direction": r["direction"], "front": r["front"], "back": r["back"]}) for r in rows}


def _remove_duplicate_cards(conn: sqlite3.Connection) -> int:
    """Remove exact duplicates, keeping the oldest card per identity."""
    cursor = conn.execute(
        """
        DELETE FROM cards
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM cards
            GROUP BY direction, front, back
        )
        """
    )
    return cursor.rowcount


SYNC_META_KEYS = (
    "last_sync_at",
    "last_sync_added",
    "last_sync_skipped",
    "last_sync_parsed",
)


def clear_sync_meta() -> None:
    init_db()
    with connect() as conn:
        for key in SYNC_META_KEYS:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def import_cards(cards: list[dict]) -> tuple[int, int, int]:
    """Insert new cards. Returns (added, skipped, total)."""
    init_db()
    added = 0
    skipped = 0
    with connect() as conn:
        _remove_duplicate_cards(conn)
        existing = _load_existing_identities(conn)
        seen_batch: set[tuple[str, str, str]] = set()

        for card in cards:
            normalized = normalize_card(card)
            identity = card_identity(normalized)

            if identity in seen_batch or identity in existing:
                if normalized.get("hint"):
                    conn.execute(
                        """
                        UPDATE cards
                        SET visual_hint = ?
                        WHERE direction = ? AND front = ? AND back = ?
                          AND (visual_hint IS NULL OR visual_hint = '')
                        """,
                        (
                            normalized.get("hint", ""),
                            normalized["direction"],
                            normalized["front"],
                            normalized["back"],
                        ),
                    )
                skipped += 1
                seen_batch.add(identity)
                continue

            seen_batch.add(identity)
            state = initial_card_state()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO cards
                    (direction, front, back, group_key, deck, source, visual_hint,
                     interval, ease, due, reps, lapses)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["direction"],
                    normalized["front"],
                    normalized["back"],
                    normalized.get("group_key", ""),
                    normalized["deck"],
                    normalized["source"],
                    normalized.get("hint", normalized.get("visual_hint", "")),
                    state["interval"],
                    state["ease"],
                    state["due"],
                    state["reps"],
                    state["lapses"],
                ),
            )
            if cursor.rowcount:
                added += 1
                existing.add(identity)
            else:
                skipped += 1

        total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    return added, skipped, total


def get_card_by_id(card_id: int) -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if not row:
            return None
        return _row_to_card(row, conn)


def _direction_filter_sql() -> str:
    """Include cards in the requested direction, plus orphan cards from the other direction."""
    return """
        direction = ?
        OR (
            direction != ?
            AND group_key != ''
            AND NOT EXISTS (
                SELECT 1 FROM cards AS other
                WHERE other.group_key = cards.group_key
                  AND other.direction = ?
            )
        )
    """


def get_card_in_group(group_key: str, direction: str) -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM cards WHERE group_key = ? AND direction = ? LIMIT 1",
            (group_key, direction),
        ).fetchone()
        if not row:
            return None
        return _row_to_card(row, conn)


def get_due_card(direction: str) -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM cards
            WHERE due <= ? AND ({_direction_filter_sql()})
            ORDER BY due ASC, reps ASC, id ASC
            LIMIT 1
            """,
            (today().isoformat(), direction, direction, direction),
        ).fetchone()
        if not row:
            return None
        return _row_to_card(row, conn)


def get_stats(direction: str | None = None) -> dict:
    init_db()
    with connect() as conn:
        if direction:
            total = conn.execute(
                f"SELECT COUNT(*) FROM cards WHERE {_direction_filter_sql()}",
                (direction, direction, direction),
            ).fetchone()[0]
            due = conn.execute(
                f"SELECT COUNT(*) FROM cards WHERE due <= ? AND ({_direction_filter_sql()})",
                (today().isoformat(), direction, direction, direction),
            ).fetchone()[0]
            new_cards = conn.execute(
                f"""
                SELECT COUNT(*) FROM cards
                WHERE reps = 0 AND ({_direction_filter_sql()})
                """,
                (direction, direction, direction),
            ).fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            due = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE due <= ?", (today().isoformat(),)
            ).fetchone()[0]
            new_cards = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE reps = 0"
            ).fetchone()[0]
    return {"total": total, "due": due, "new": new_cards}


def get_direction_counts() -> dict[str, int]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT direction, COUNT(*) AS count FROM cards GROUP BY direction"
        ).fetchall()
    counts = {"en_de": 0, "de_en": 0}
    for row in rows:
        counts[row["direction"]] = row["count"]
    return counts


def reset_all_progress(direction: str | None = None) -> int:
    """Reset scheduling on cards. Returns number of cards affected."""
    init_db()
    state = initial_card_state()
    with connect() as conn:
        if direction:
            cursor = conn.execute(
                """
                UPDATE cards
                SET interval = ?, ease = ?, due = ?, reps = ?, lapses = ?
                WHERE direction = ?
                """,
                (
                    state["interval"],
                    state["ease"],
                    state["due"],
                    state["reps"],
                    state["lapses"],
                    direction,
                ),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE cards
                SET interval = ?, ease = ?, due = ?, reps = ?, lapses = ?
                """,
                (
                    state["interval"],
                    state["ease"],
                    state["due"],
                    state["reps"],
                    state["lapses"],
                ),
            )
        return cursor.rowcount


def clear_all_cards() -> int:
    """Delete all imported phrases. Returns number of cards removed."""
    init_db()
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        conn.execute("DELETE FROM cards")
    clear_sync_meta()
    return count


def update_card(card_id: int, fields: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE cards
            SET interval = ?, ease = ?, due = ?, reps = ?, lapses = ?
            WHERE id = ?
            """,
            (
                fields["interval"],
                fields["ease"],
                fields["due"],
                fields["reps"],
                fields["lapses"],
                card_id,
            ),
        )


def _group_directions(conn: sqlite3.Connection, group_key: str, direction: str) -> list[str]:
    if not group_key:
        return [direction]
    rows = conn.execute(
        "SELECT DISTINCT direction FROM cards WHERE group_key = ? ORDER BY direction",
        (group_key,),
    ).fetchall()
    paired = [row["direction"] for row in rows]
    return paired or [direction]


def _row_to_card(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    raw_hint = row["visual_hint"] if "visual_hint" in row.keys() else ""
    hint_value, hint_type = classify_hint(raw_hint)
    paired_directions = _group_directions(conn, row["group_key"], row["direction"])
    return {
        "id": row["id"],
        "front": row["front"],
        "back": row["back"],
        "deck": row["deck"],
        "direction": row["direction"],
        "group_key": row["group_key"],
        "hint": hint_value,
        "hint_type": hint_type,
        "paired_directions": paired_directions,
        "can_switch_direction": len(paired_directions) > 1,
        "interval": row["interval"],
        "ease": row["ease"],
        "due": row["due"],
        "reps": row["reps"],
        "lapses": row["lapses"],
    }
