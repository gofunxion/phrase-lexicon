"""Parse flashcards from the Deutsch Google Doc export."""

import hashlib
import base64
import os
import re
import urllib.request
from pathlib import Path

DOC_ID = os.environ.get(
    "GOOGLE_DOC_ID", "1TX2Qd17AJ9nQ_A3QUtNSNbQ5WEqt4hfFVoaAUD_ifCw"
).strip()
DEFAULT_DOC_TAB = "t.x0jh4b5vn4op"

_data_dir = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
HINTS_DIR = _data_dir / "hints"
HINTS_DIR.mkdir(parents=True, exist_ok=True)


def normalize_tab_id(tab: str) -> str:
    tab = tab.strip()
    if not tab:
        return ""
    if not tab.startswith("t."):
        tab = f"t.{tab}"
    return tab


def current_doc_tab() -> str:
    return normalize_tab_id(os.environ.get("GOOGLE_DOC_TAB", DEFAULT_DOC_TAB))


def export_url(doc_id: str | None = None, doc_tab: str | None = None) -> str:
    doc_id = (doc_id or DOC_ID).strip()
    tab = normalize_tab_id(doc_tab if doc_tab is not None else current_doc_tab())
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    if tab:
        url += f"&tab={tab}"
    return url


def doc_source() -> dict[str, str]:
    tab = current_doc_tab()
    return {
        "doc_id": DOC_ID,
        "doc_tab": tab,
        "export_url": export_url(),
    }


def fetch_doc(url: str | None = None) -> str:
    if url is None:
        url = export_url()
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def fetch_doc_html() -> str:
    url = export_url().replace("format=txt", "format=html")
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _persist_hint_src(src: str) -> str:
    if not src.startswith("data:image/"):
        return src

    header, _, payload = src.partition(",")
    if not payload:
        return ""

    raw = base64.b64decode(payload)
    ext = "png"
    if "image/jpeg" in header or "image/jpg" in header:
        ext = "jpg"
    elif "image/gif" in header:
        ext = "gif"
    elif "image/webp" in header:
        ext = "webp"

    digest = hashlib.sha1(raw).hexdigest()[:16]
    path = HINTS_DIR / f"{digest}.{ext}"
    if not path.exists():
        path.write_bytes(raw)
    return f"/api/hints/{digest}.{ext}"


ENGLISH_LINE = re.compile(
    r"^(to |a |an |the |no |slow |social |free |fuel |perception|please )",
    re.I,
)

IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>\"']+"
    r"(?:\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s<>\"']*)?"
    r"|(?:docs\.google\.com|googleusercontent\.com|ggpht\.com)[^\s<>\"']*)",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def normalize_card(card: dict) -> dict:
    direction = card["direction"].strip().lower()
    if direction not in ("en_de", "de_en"):
        direction = "en_de"
    hint = normalize_text(card.get("hint", card.get("visual_hint", "")))
    hint_value, hint_type = classify_hint(hint)
    return {
        **card,
        "direction": direction,
        "front": normalize_text(card["front"]),
        "back": normalize_text(card["back"]),
        "hint": hint_value,
        "hint_type": hint_type,
    }


def classify_hint(text: str) -> tuple[str, str]:
    text = normalize_text(text) if text else ""
    if not text:
        return "", "none"

    if text.startswith("data:image/") or text.startswith("/api/hints/"):
        return text, "image"

    image_match = IMAGE_URL_RE.search(text)
    if image_match:
        return image_match.group(0).rstrip(".,;)"), "image"

    url_match = re.search(r"https?://\S+", text)
    if url_match:
        url = url_match.group(0).rstrip(".,;)")
        if re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", url, re.I):
            return url, "image"

    return text, "text"


def card_identity(card: dict) -> tuple[str, str, str]:
    normalized = normalize_card(card)
    return normalized["direction"], normalized["front"], normalized["back"]


def _group_key(en_de_front: str, de_en_front: str) -> str:
    raw = f"{en_de_front}|{de_en_front}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _parse_direction_content(content: str) -> dict[str, str]:
    front = ""
    back = ""
    hint_lines: list[str] = []
    in_hint = False

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            if in_hint:
                in_hint = False
            continue

        if re.match(r"^Front:\s*", stripped, re.I):
            in_hint = False
            front = re.sub(r"^Front:\s*", "", stripped, flags=re.I)
        elif re.match(r"^Back:\s*", stripped, re.I):
            in_hint = False
            back = re.sub(r"^Back:\s*", "", stripped, flags=re.I)
        elif re.match(r"^Note:\s*", stripped, re.I):
            in_hint = False
        elif re.match(r"^(?:Hint|Visual hint):\s*", stripped, re.I):
            in_hint = True
            rest = re.sub(r"^(?:Hint|Visual hint):\s*", "", stripped, flags=re.I)
            if rest:
                hint_lines.append(rest)
        elif in_hint:
            hint_lines.append(stripped)
        elif stripped.upper() in ("EN_DE", "DE_EN"):
            break

    return {
        "front": front,
        "back": back,
        "hint": normalize_text(" ".join(hint_lines)),
    }


def _block_hints(sections: dict[str, dict[str, str]]) -> dict[str, str]:
    """Image hints are shared across directions; text hints stay per direction."""
    shared_image = ""
    for payload in sections.values():
        value, hint_type = classify_hint(payload.get("hint", ""))
        if hint_type == "image":
            shared_image = value
            break

    if shared_image:
        return {direction: shared_image for direction in sections}

    return {direction: payload.get("hint", "") for direction, payload in sections.items()}


def _extract_html_image_hints(html: str) -> list[str]:
    hints: list[str] = []
    for part in re.split(r"Hint:</span>", html, flags=re.IGNORECASE)[1:]:
        img = re.search(r'<img[^>]+src="([^"]+)"', part, re.IGNORECASE)
        hints.append(img.group(1) if img else "")
    return hints


def _apply_group_image_hints(cards: list[dict]) -> None:
    by_group: dict[str, list[dict]] = {}
    for card in cards:
        by_group.setdefault(card["group_key"], []).append(card)

    for group in by_group.values():
        shared_image = ""
        for card in group:
            _, hint_type = classify_hint(card.get("hint", ""))
            if hint_type == "image":
                shared_image = card["hint"]
                break
        if shared_image:
            for card in group:
                card["hint"] = shared_image


def merge_html_image_hints(cards: list[dict], html: str) -> None:
    images = _extract_html_image_hints(html)
    img_idx = 0
    for card in cards:
        if card.get("hint"):
            continue
        while img_idx < len(images) and not images[img_idx]:
            img_idx += 1
        if img_idx >= len(images):
            break
        card["hint"] = _persist_hint_src(images[img_idx])
        img_idx += 1
    _apply_group_image_hints(cards)


def parse_cloze_block(block: str) -> list[dict]:
    block = block.strip()
    if not block or block.startswith("Phrase Lexicon"):
        return []

    parts = re.split(r"^(EN_DE|DE_EN)\s*$", block, flags=re.MULTILINE | re.IGNORECASE)
    sections: dict[str, dict[str, str]] = {}
    for index in range(1, len(parts), 2):
        direction = parts[index].lower()
        sections[direction] = _parse_direction_content(parts[index + 1])

    if not sections:
        return []

    group_key = _group_key(
        sections.get("en_de", {}).get("front", block[:80]),
        sections.get("de_en", {}).get("front", block[:80]),
    )
    hints = _block_hints(sections)

    cards = []
    for direction, payload in sections.items():
        if payload["front"] and payload["back"]:
            cards.append(
                {
                    "direction": direction,
                    "front": payload["front"],
                    "back": payload["back"],
                    "hint": hints.get(direction, ""),
                    "group_key": group_key,
                    "deck": "phrase_lexicon",
                    "source": "cloze",
                }
            )
    return cards


def _card_section(text: str) -> tuple[str, str]:
    for heading in ("Trainer", "Phrase Trainer"):
        if heading in text:
            section = text.split(heading, 1)[1]
            if "Phrase Lexicon" in section:
                section = section.split("Phrase Lexicon", 1)[0]
            return section, "trainer"

    if "Phrase Lexicon" in text:
        return text.split("Phrase Lexicon", 1)[1], "phrase_lexicon"

    # Dedicated trainer tab export: cloze blocks without a section heading.
    if re.search(r"^(EN_DE|DE_EN)\s*$", text, re.MULTILINE | re.IGNORECASE):
        return text, "trainer"

    return text, "phrase_lexicon"


def parse_cloze_cards(text: str) -> list[dict]:
    section, deck = _card_section(text)

    cards: list[dict] = []
    for block in re.split(r"^---\s*$", section, flags=re.MULTILINE):
        for card in parse_cloze_block(block):
            card["deck"] = deck
            cards.append(card)
    return cards


def parse_phrase_lexicon_legacy(text: str) -> list[dict]:
    if "Phrase Lexicon" not in text:
        return []

    section = text.split("Phrase Lexicon", 1)[1]
    if "---" in section:
        return []

    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]

    cards = []
    i = 0
    while i < len(lines) - 1:
        front, back = lines[i], lines[i + 1]
        if ENGLISH_LINE.match(front) or "/" in front:
            group_key = hashlib.sha1(f"{front}|{back}".encode()).hexdigest()[:16]
            cards.append(
                {
                    "direction": "en_de",
                    "front": front,
                    "back": back,
                    "group_key": group_key,
                    "deck": "phrase_lexicon",
                    "source": "phrase_lexicon_legacy",
                }
            )
            cards.append(
                {
                    "direction": "de_en",
                    "front": back,
                    "back": front,
                    "group_key": group_key,
                    "deck": "phrase_lexicon",
                    "source": "phrase_lexicon_legacy",
                }
            )
            i += 2
        else:
            i += 1
    return cards


def parse_gwod_cards(text: str) -> list[dict]:
    cards = []
    for match in re.finditer(
        r"Today's GWoD:\s*(.+?)\s*\n(?:.*?\n){0,80}?^WU:\s*(.+)$",
        text,
        re.MULTILINE,
    ):
        word = re.sub(r"\s*\([^)]+\)\s*$", "", match.group(1)).strip()
        meaning = match.group(2).strip()
        if word and meaning:
            group_key = hashlib.sha1(f"{meaning}|{word}".encode()).hexdigest()[:16]
            cards.append(
                {
                    "direction": "en_de",
                    "front": meaning,
                    "back": word,
                    "group_key": group_key,
                    "deck": "gwod",
                    "source": "gwod",
                }
            )
            cards.append(
                {
                    "direction": "de_en",
                    "front": word,
                    "back": meaning,
                    "group_key": group_key,
                    "deck": "gwod",
                    "source": "gwod",
                }
            )
    return cards


def parse_all(text: str | None = None) -> list[dict]:
    html: str | None = None
    if text is None:
        text = fetch_doc()
        try:
            html = fetch_doc_html()
        except Exception:
            html = None

    cloze = parse_cloze_cards(text)
    if html and cloze:
        merge_html_image_hints(cloze, html)
    legacy = parse_phrase_lexicon_legacy(text) if not cloze else []
    cards = cloze + legacy + parse_gwod_cards(text)

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for card in cards:
        key = card_identity(card)
        if key not in seen:
            seen.add(key)
            unique.append(normalize_card(card))
    return unique
