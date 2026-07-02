"""Phrase lexicon trainer — Anki-style review from Google Doc."""

import os

from flask import Flask, jsonify, render_template, request, send_from_directory

import db
from scheduler import review_card
from sync_service import run_sync
from parser import HINTS_DIR, doc_source

app = Flask(__name__)
BUILD_ID = os.environ.get("BUILD_ID", "ae84807-3")


@app.after_request
def disable_html_cache(response):
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _sync_response(force: bool = False) -> dict:
    try:
        return run_sync(force=force)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.route("/")
def index():
    db.init_db()
    direction = request.args.get("direction", "de_en")
    if direction not in ("en_de", "de_en"):
        direction = "de_en"
    stats = db.get_stats(direction)
    return render_template("review.html", stats=stats, direction=direction, build_id=BUILD_ID)


@app.route("/api/sync", methods=["POST"])
def sync():
    result = _sync_response(force=True)
    if not result.get("ok"):
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/sync/auto", methods=["POST"])
def sync_auto():
    result = _sync_response(force=False)
    if not result.get("ok"):
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/next")
def next_card():
    direction = request.args.get("direction", "de_en")
    if direction not in ("en_de", "de_en"):
        direction = "de_en"
    card = db.get_due_card(direction)
    if not card:
        return jsonify(
            {
                "ok": True,
                "card": None,
                "stats": db.get_stats(direction),
                "direction_counts": db.get_direction_counts(),
            }
        )
    return jsonify(
        {
            "ok": True,
            "card": card,
            "stats": db.get_stats(direction),
            "direction_counts": db.get_direction_counts(),
        }
    )


@app.route("/api/card-for-group")
def card_for_group():
    group_key = request.args.get("group_key", "")
    direction = request.args.get("direction", "de_en")
    if direction not in ("en_de", "de_en"):
        direction = "de_en"
    if not group_key:
        return jsonify({"ok": False, "error": "Missing group_key"}), 400
    card = db.get_card_in_group(group_key, direction)
    if not card:
        return jsonify({"ok": True, "card": None})
    return jsonify({"ok": True, "card": card})


@app.route("/api/review", methods=["POST"])
def review():
    data = request.get_json(force=True)
    card_id = data.get("id")
    rating = data.get("rating")
    direction = data.get("direction", "de_en")
    study_direction = data.get("study_direction", direction)

    if card_id is None or rating not in (0, 1, 2, 3):
        return jsonify({"ok": False, "error": "Invalid review payload"}), 400

    card = db.get_card_by_id(card_id)
    if not card:
        return jsonify({"ok": False, "error": "Card not found"}), 404
    if card["direction"] != direction:
        return jsonify({"ok": False, "error": "Card direction mismatch"}), 400

    updated = review_card(card, rating)
    db.update_card(card_id, updated)
    return jsonify({"ok": True, "stats": db.get_stats(study_direction)})


@app.route("/api/hints/<path:filename>")
def hint_image(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return send_from_directory(HINTS_DIR, filename)


@app.route("/api/status")
def status():
    direction = request.args.get("direction", "de_en")
    if direction not in ("en_de", "de_en"):
        direction = "de_en"
    return jsonify(
        {
            "ok": True,
            "stats": db.get_stats(direction),
            "direction_counts": db.get_direction_counts(),
            "last_sync_at": db.get_meta("last_sync_at"),
            "doc_source": doc_source(),
        }
    )


@app.route("/api/reset-progress", methods=["POST"])
def reset_progress():
    data = request.get_json(force=True)
    if data.get("confirmation") != "RESET":
        return jsonify(
            {
                "ok": False,
                "error": "Type the word RESET to confirm.",
            }
        ), 400

    count = db.reset_all_progress()
    return jsonify({"ok": True, "reset": count, "stats": db.get_stats()})


@app.route("/api/reset-dict", methods=["POST"])
def reset_dict():
    data = request.get_json(force=True)
    if data.get("confirmation") != "DELETE":
        return jsonify(
            {
                "ok": False,
                "error": "Type the word DELETE to confirm.",
            }
        ), 400

    count = db.clear_all_cards()
    return jsonify(
        {
            "ok": True,
            "deleted": count,
            "stats": db.get_stats(),
            "direction_counts": db.get_direction_counts(),
        }
    )


@app.route("/api/cron/sync")
def cron_sync():
    secret = os.environ.get("CRON_SECRET")
    if secret and request.args.get("key") != secret:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    result = _sync_response(force=True)
    if not result.get("ok"):
        return jsonify(result), 500
    return jsonify(result)


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000, host="0.0.0.0")
