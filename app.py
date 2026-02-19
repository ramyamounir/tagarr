import os
import re
import sqlite3
import unicodedata

from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DB_PATH = os.environ.get("SONARR_DB", "/data/sonarr/sonarr.db")
MANUAL_TYPE = "ManualMapping"
MANUAL_ORIGIN = "manual"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def clean_series_title(title):
    if not title or not title.strip():
        return title
    if title.isdigit():
        return title
    title = title.replace("%", "percent")
    title = re.sub(
        r"(?:(?<=\b)|(?<=_))(?<!^)(?:a(?!$)|à(?!$)|an|the|and|or|of)(?!$)(?=\b|_)",
        "", title, flags=re.IGNORECASE | re.UNICODE,
    )
    title = re.sub(r"[\W_]+", "", title, flags=re.UNICODE)
    title = title.lower()
    normalized = unicodedata.normalize("NFD", title)
    title = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return title


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1 FROM Series LIMIT 1")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


@app.route("/api/search")
def search():
    term = request.args.get("q", "").strip()
    if not term:
        return jsonify([])

    with get_db() as conn:
        series_rows = conn.execute(
            'SELECT "TvdbId", "Title", "Year", "Status"'
            ' FROM "Series"'
            ' WHERE "Title" LIKE ? OR "CleanTitle" LIKE ?'
            ' ORDER BY "Title"',
            (f"%{term}%", f"%{term.lower().replace(' ', '')}%"),
        ).fetchall()

        results = []
        for s in series_rows:
            aliases = conn.execute(
                'SELECT "Id", "Title", "SearchTerm", "SeasonNumber", "Type"'
                ' FROM "SceneMappings"'
                ' WHERE "TvdbId" = ?'
                ' ORDER BY "Type", "Id"',
                (s["TvdbId"],),
            ).fetchall()

            results.append({
                "tvdb_id": s["TvdbId"],
                "title": s["Title"],
                "year": s["Year"],
                "status": s["Status"],
                "aliases": [{
                    "id": a["Id"],
                    "title": a["Title"],
                    "search_term": a["SearchTerm"],
                    "season": a["SeasonNumber"] if a["SeasonNumber"] is not None and a["SeasonNumber"] >= 0 else None,
                    "manual": a["Type"] == MANUAL_TYPE,
                } for a in aliases],
            })

    return jsonify(results)


@app.route("/api/alias", methods=["POST"])
def add_alias():
    data = request.json
    tvdb_id = data.get("tvdb_id")
    title = data.get("title", "").strip()
    search_term = data.get("search_term", "").strip() or title
    season = data.get("season")

    if not tvdb_id or not title:
        return jsonify({"error": "tvdb_id and title are required"}), 400

    parse_term = clean_series_title(title)
    if not parse_term:
        return jsonify({"error": "Title normalizes to an empty string"}), 400

    with get_db() as conn:
        existing = conn.execute(
            'SELECT "Id" FROM "SceneMappings" WHERE "ParseTerm" = ? AND "TvdbId" = ? AND "Type" = ?',
            (parse_term, tvdb_id, MANUAL_TYPE),
        ).fetchone()

        if existing:
            return jsonify({"error": "This alias already exists for this series"}), 409

        conn.execute(
            'INSERT INTO "SceneMappings"'
            ' ("Title", "ParseTerm", "SearchTerm", "TvdbId", "SeasonNumber",'
            '  "SceneSeasonNumber", "SceneOrigin", "SearchMode", "Comment",'
            '  "FilterRegex", "Type")'
            ' VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, NULL, ?)',
            (
                title,
                parse_term,
                search_term,
                tvdb_id,
                season if season is not None else -1,
                MANUAL_ORIGIN,
                "Manual alias",
                MANUAL_TYPE,
            ),
        )
        conn.commit()

    return jsonify({"ok": True})


@app.route("/api/alias/<int:alias_id>", methods=["DELETE"])
def remove_alias(alias_id):
    with get_db() as conn:
        row = conn.execute(
            'SELECT "Title", "Type" FROM "SceneMappings" WHERE "Id" = ?',
            (alias_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Alias not found"}), 404

        if row["Type"] != MANUAL_TYPE:
            return jsonify({"error": "Only manual aliases can be removed"}), 403

        conn.execute('DELETE FROM "SceneMappings" WHERE "Id" = ?', (alias_id,))
        conn.commit()

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
