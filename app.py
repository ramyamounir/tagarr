import os
import re
import sqlite3
import unicodedata

from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

SONARR_DB = os.environ.get("SONARR_DB", "")
RADARR_DB = os.environ.get("RADARR_DB", "")

SONARR_MANUAL_TYPE = "ManualMapping"
SONARR_MANUAL_ORIGIN = "manual"
RADARR_MANUAL_SOURCE_TYPE = 2

SONARR_STATUS = {0: "Continuing", 1: "Ended"}
RADARR_STATUS = {0: "TBA", 1: "Announced", 2: "In Cinemas", 3: "Released"}


def _get_db(path, readonly=False):
    if not path or not os.path.isfile(path):
        return None
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_sonarr_db(readonly=False):
    return _get_db(SONARR_DB, readonly=readonly)


def get_radarr_db(readonly=False):
    return _get_db(RADARR_DB, readonly=readonly)


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
    status = {}
    any_ok = False

    conn = get_sonarr_db(readonly=True)
    if conn:
        try:
            conn.execute("SELECT 1 FROM Series LIMIT 1")
            status["sonarr"] = "ok"
            any_ok = True
        except Exception as e:
            status["sonarr"] = f"error: {e}"
        finally:
            conn.close()
    else:
        status["sonarr"] = "not configured"

    conn = get_radarr_db(readonly=True)
    if conn:
        try:
            conn.execute("SELECT 1 FROM Movies LIMIT 1")
            status["radarr"] = "ok"
            any_ok = True
        except Exception as e:
            status["radarr"] = f"error: {e}"
        finally:
            conn.close()
    else:
        status["radarr"] = "not configured"

    code = 200 if any_ok else 503
    return jsonify({"status": "ok" if any_ok else "error", "databases": status}), code


def search_sonarr(term):
    conn = get_sonarr_db(readonly=True)
    if not conn:
        return []

    results = []
    try:
        series_rows = conn.execute(
            'SELECT "TvdbId", "Title", "Year", "Status"'
            ' FROM "Series"'
            ' WHERE "Title" LIKE ? OR "CleanTitle" LIKE ?'
            ' ORDER BY "Title"',
            (f"%{term}%", f"%{term.lower().replace(' ', '')}%"),
        ).fetchall()

        for s in series_rows:
            aliases = conn.execute(
                'SELECT "Id", "Title", "SearchTerm", "SeasonNumber", "Type"'
                ' FROM "SceneMappings"'
                ' WHERE "TvdbId" = ?'
                ' ORDER BY "Type", "Id"',
                (s["TvdbId"],),
            ).fetchall()

            results.append({
                "source": "sonarr",
                "media_type": "series",
                "media_id": s["TvdbId"],
                "title": s["Title"],
                "year": s["Year"],
                "status": SONARR_STATUS.get(s["Status"], str(s["Status"])),
                "aliases": [{
                    "id": a["Id"],
                    "title": a["Title"],
                    "search_term": a["SearchTerm"],
                    "season": a["SeasonNumber"] if a["SeasonNumber"] is not None and a["SeasonNumber"] >= 0 else None,
                    "manual": a["Type"] == SONARR_MANUAL_TYPE,
                } for a in aliases],
            })
    finally:
        conn.close()
    return results


def search_radarr(term):
    conn = get_radarr_db(readonly=True)
    if not conn:
        return []

    results = []
    try:
        movie_rows = conn.execute(
            'SELECT m."Id" AS "MovieId", mm."Id" AS "MetadataId",'
            ' mm."Title", mm."Year", mm."Status", mm."TmdbId"'
            ' FROM "Movies" m'
            ' JOIN "MovieMetadata" mm ON m."MovieMetadataId" = mm."Id"'
            ' WHERE mm."Title" LIKE ? OR mm."CleanTitle" LIKE ?'
            ' ORDER BY mm."Title"',
            (f"%{term}%", f"%{term.lower().replace(' ', '')}%"),
        ).fetchall()

        for m in movie_rows:
            aliases = conn.execute(
                'SELECT "Id", "Title", "CleanTitle", "SourceType"'
                ' FROM "AlternativeTitles"'
                ' WHERE "MovieMetadataId" = ?'
                ' ORDER BY "SourceType", "Id"',
                (m["MetadataId"],),
            ).fetchall()

            results.append({
                "source": "radarr",
                "media_type": "movie",
                "media_id": m["TmdbId"],
                "metadata_id": m["MetadataId"],
                "title": m["Title"],
                "year": m["Year"],
                "status": RADARR_STATUS.get(m["Status"], str(m["Status"])),
                "aliases": [{
                    "id": a["Id"],
                    "title": a["Title"],
                    "search_term": None,
                    "season": None,
                    "manual": a["SourceType"] == RADARR_MANUAL_SOURCE_TYPE,
                } for a in aliases],
            })
    finally:
        conn.close()
    return results


@app.route("/api/search")
def search():
    term = request.args.get("q", "").strip()
    if not term:
        return jsonify([])

    results = search_sonarr(term) + search_radarr(term)
    results.sort(key=lambda r: r["title"].lower())
    return jsonify(results)


def _add_sonarr_alias(data):
    tvdb_id = data.get("tvdb_id")
    title = data.get("title", "").strip()
    search_term = data.get("search_term", "").strip() or title
    season = data.get("season")

    if not tvdb_id or not title:
        return jsonify({"error": "tvdb_id and title are required"}), 400

    parse_term = clean_series_title(title)
    if not parse_term:
        return jsonify({"error": "Title normalizes to an empty string"}), 400

    conn = get_sonarr_db()
    if not conn:
        return jsonify({"error": "Sonarr database not configured"}), 503

    with conn:
        existing = conn.execute(
            'SELECT "Id" FROM "SceneMappings" WHERE "ParseTerm" = ? AND "TvdbId" = ? AND "Type" = ?',
            (parse_term, tvdb_id, SONARR_MANUAL_TYPE),
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
                SONARR_MANUAL_ORIGIN,
                "Manual alias",
                SONARR_MANUAL_TYPE,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


def _add_radarr_alias(data):
    metadata_id = data.get("metadata_id")
    title = data.get("title", "").strip()

    if not metadata_id or not title:
        return jsonify({"error": "metadata_id and title are required"}), 400

    clean_title = clean_series_title(title)
    if not clean_title:
        return jsonify({"error": "Title normalizes to an empty string"}), 400

    conn = get_radarr_db()
    if not conn:
        return jsonify({"error": "Radarr database not configured"}), 503

    with conn:
        existing = conn.execute(
            'SELECT "Id" FROM "AlternativeTitles"'
            ' WHERE "CleanTitle" = ? AND "MovieMetadataId" = ? AND "SourceType" = ?',
            (clean_title, metadata_id, RADARR_MANUAL_SOURCE_TYPE),
        ).fetchone()

        if existing:
            return jsonify({"error": "This alias already exists for this movie"}), 409

        conn.execute(
            'INSERT INTO "AlternativeTitles"'
            ' ("Title", "CleanTitle", "SourceType", "MovieMetadataId")'
            ' VALUES (?, ?, ?, ?)',
            (title, clean_title, RADARR_MANUAL_SOURCE_TYPE, metadata_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


@app.route("/api/alias", methods=["POST"])
def add_alias():
    data = request.json
    source = data.get("source", "sonarr")

    if source == "radarr":
        return _add_radarr_alias(data)
    return _add_sonarr_alias(data)


def _remove_sonarr_alias(alias_id):
    conn = get_sonarr_db()
    if not conn:
        return jsonify({"error": "Sonarr database not configured"}), 503

    with conn:
        row = conn.execute(
            'SELECT "Title", "Type" FROM "SceneMappings" WHERE "Id" = ?',
            (alias_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Alias not found"}), 404

        if row["Type"] != SONARR_MANUAL_TYPE:
            return jsonify({"error": "Only manual aliases can be removed"}), 403

        conn.execute('DELETE FROM "SceneMappings" WHERE "Id" = ?', (alias_id,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


def _remove_radarr_alias(alias_id):
    conn = get_radarr_db()
    if not conn:
        return jsonify({"error": "Radarr database not configured"}), 503

    with conn:
        row = conn.execute(
            'SELECT "Title", "SourceType" FROM "AlternativeTitles" WHERE "Id" = ?',
            (alias_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Alias not found"}), 404

        if row["SourceType"] != RADARR_MANUAL_SOURCE_TYPE:
            return jsonify({"error": "Only manual aliases can be removed"}), 403

        conn.execute('DELETE FROM "AlternativeTitles" WHERE "Id" = ?', (alias_id,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


@app.route("/api/alias/<int:alias_id>", methods=["DELETE"])
def remove_alias(alias_id):
    source = request.args.get("source", "sonarr")

    if source == "radarr":
        return _remove_radarr_alias(alias_id)
    return _remove_sonarr_alias(alias_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
