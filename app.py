import os
import re
import sqlite3
import unicodedata

from flask import Flask, jsonify, request, render_template, send_file, abort

app = Flask(__name__)

SONARR_DB = os.environ.get("SONARR_DB", "")
RADARR_DB = os.environ.get("RADARR_DB", "")

SONARR_MANUAL_TYPE = "ManualMapping"
SONARR_MANUAL_ORIGIN = "manual"
SONARR_SEARCH_MODE_BOTH = 3  # SearchID(1) | SearchTitle(2) — search by both ID and title simultaneously
RADARR_MANUAL_SOURCE_TYPE = 2

SONARR_STATUS = {0: "Continuing", 1: "Ended"}
RADARR_STATUS = {0: "TBA", 1: "Announced", 2: "In Cinemas", 3: "Released"}


def _get_db(path, readonly=False):
    if not path or not os.path.isfile(path):
        return None
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
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


def clean_movie_title(title):
    if not title or not title.strip():
        return title
    if title.isdigit():
        return title
    title = title.replace("%", "percent")
    # German umlaut expansion (Radarr's CleanMovieTitle does this before stripping diacritics)
    for src, repl in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"),
                      ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        title = title.replace(src, repl)
    title = re.sub(
        r"(?:(?<=\b)|(?<=_))(?<!^)(?:a(?!$)|à(?!$)|an|the|and|or|of)(?!$)(?=\b|_)",
        "", title, flags=re.IGNORECASE | re.UNICODE,
    )
    title = re.sub(r"[\W_]+", "", title, flags=re.UNICODE)
    title = title.lower()
    normalized = unicodedata.normalize("NFD", title)
    title = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return title


def _ensure_tagarr_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS "TagarrAliases" ('
        ' "Id" INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' "Title" TEXT NOT NULL,'
        ' "CleanTitle" TEXT NOT NULL,'
        ' "MovieMetadataId" INTEGER NOT NULL,'
        ' UNIQUE ("MovieMetadataId", "CleanTitle"))'
    )
    # Backfill any existing manual aliases we don't yet track
    conn.execute(
        'INSERT OR IGNORE INTO "TagarrAliases" ("Title", "CleanTitle", "MovieMetadataId")'
        ' SELECT "Title", "CleanTitle", "MovieMetadataId"'
        ' FROM "AlternativeTitles"'
        ' WHERE "SourceType" = ?',
        (RADARR_MANUAL_SOURCE_TYPE,),
    )
    conn.commit()


def _sync_radarr_aliases(conn):
    _ensure_tagarr_table(conn)

    missing = conn.execute(
        'SELECT t."Title", t."CleanTitle", t."MovieMetadataId"'
        ' FROM "TagarrAliases" t'
        ' LEFT JOIN "AlternativeTitles" a'
        '   ON a."MovieMetadataId" = t."MovieMetadataId"'
        '   AND a."CleanTitle" = t."CleanTitle"'
        ' WHERE a."Id" IS NULL'
    ).fetchall()

    if not missing:
        return

    for row in missing:
        conn.execute(
            'INSERT INTO "AlternativeTitles"'
            ' ("Title", "CleanTitle", "SourceType", "MovieMetadataId")'
            ' VALUES (?, ?, ?, ?)',
            (row["Title"], row["CleanTitle"], RADARR_MANUAL_SOURCE_TYPE, row["MovieMetadataId"]),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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


@app.route("/media/poster/<source>/<int:db_id>")
def poster(source, db_id):
    if source == "sonarr":
        base = os.path.dirname(SONARR_DB) if SONARR_DB else ""
    elif source == "radarr":
        base = os.path.dirname(RADARR_DB) if RADARR_DB else ""
    else:
        abort(404)

    if not base:
        abort(404)

    path = os.path.join(base, "MediaCover", str(db_id), "poster-250.jpg")
    if not os.path.isfile(path):
        abort(404)

    return send_file(path, mimetype="image/jpeg", max_age=86400)


def search_sonarr(term):
    conn = get_sonarr_db(readonly=True)
    if not conn:
        return []

    results = []
    try:
        series_rows = conn.execute(
            'SELECT "Id", "TvdbId", "Title", "Year", "Status"'
            ' FROM "Series"'
            ' WHERE "Title" LIKE ? OR "CleanTitle" LIKE ?'
            ' ORDER BY "Title"',
            (f"%{term}%", f"%{term.lower().replace(' ', '')}%"),
        ).fetchall()

        for s in series_rows:
            aliases = conn.execute(
                'SELECT "Id", "Title", "SearchTerm", "SeasonNumber", "Type", "Comment"'
                ' FROM "SceneMappings"'
                ' WHERE "TvdbId" = ?'
                ' ORDER BY "Type", "Id"',
                (s["TvdbId"],),
            ).fetchall()

            alias_list = []
            seen_comments = set()
            for a in aliases:
                comment = a["Comment"] or ""
                is_manual = a["Type"] == SONARR_MANUAL_TYPE
                if is_manual and comment.startswith("network:"):
                    if comment in seen_comments:
                        continue
                    seen_comments.add(comment)
                    # Extract network name: "network:NET|parseterm"
                    payload = comment[len("network:"):]
                    network = payload.split("|", 1)[0]
                    # Find the base title (shorter one in the pair)
                    pair = [r for r in aliases if (r["Comment"] or "") == comment]
                    base = min(pair, key=lambda r: len(r["Title"]))
                    alias_list.append({
                        "id": base["Id"],
                        "title": base["Title"],
                        "search_term": base["SearchTerm"],
                        "network": network,
                        "manual": True,
                    })
                else:
                    alias_list.append({
                        "id": a["Id"],
                        "title": a["Title"],
                        "search_term": a["SearchTerm"],
                        "network": None,
                        "manual": is_manual,
                    })

            results.append({
                "source": "sonarr",
                "media_type": "series",
                "db_id": s["Id"],
                "media_id": s["TvdbId"],
                "title": s["Title"],
                "year": s["Year"],
                "status": SONARR_STATUS.get(s["Status"], str(s["Status"])),
                "aliases": alias_list,
            })
    finally:
        conn.close()
    return results


def search_radarr(term):
    conn = get_radarr_db()
    if not conn:
        return []

    results = []
    try:
        _sync_radarr_aliases(conn)

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
                "db_id": m["MovieId"],
                "media_id": m["TmdbId"],
                "metadata_id": m["MetadataId"],
                "title": m["Title"],
                "year": m["Year"],
                "status": RADARR_STATUS.get(m["Status"], str(m["Status"])),
                "aliases": [{
                    "id": a["Id"],
                    "title": a["Title"],
                    "search_term": None,
                    "network": None,
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
    network = (data.get("network") or "").strip()

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

        if network:
            net_title = f"{title} {network}"
            net_parse_term = clean_series_title(net_title)
            net_search_term = net_title

            existing_net = conn.execute(
                'SELECT "Id" FROM "SceneMappings" WHERE "ParseTerm" = ? AND "TvdbId" = ? AND "Type" = ?',
                (net_parse_term, tvdb_id, SONARR_MANUAL_TYPE),
            ).fetchone()

            if existing_net:
                return jsonify({"error": "This alias already exists for this series"}), 409

            comment = f"network:{network}|{parse_term}"

            # Base title entry
            conn.execute(
                'INSERT INTO "SceneMappings"'
                ' ("Title", "ParseTerm", "SearchTerm", "TvdbId", "SeasonNumber",'
                '  "SceneSeasonNumber", "SceneOrigin", "SearchMode", "Comment",'
                '  "FilterRegex", "Type")'
                ' VALUES (?, ?, ?, ?, -1, NULL, ?, ?, ?, NULL, ?)',
                (title, parse_term, search_term, tvdb_id,
                 SONARR_MANUAL_ORIGIN, SONARR_SEARCH_MODE_BOTH, comment, SONARR_MANUAL_TYPE),
            )
            # Network title entry
            conn.execute(
                'INSERT INTO "SceneMappings"'
                ' ("Title", "ParseTerm", "SearchTerm", "TvdbId", "SeasonNumber",'
                '  "SceneSeasonNumber", "SceneOrigin", "SearchMode", "Comment",'
                '  "FilterRegex", "Type")'
                ' VALUES (?, ?, ?, ?, -1, NULL, ?, ?, ?, NULL, ?)',
                (net_title, net_parse_term, net_search_term, tvdb_id,
                 SONARR_MANUAL_ORIGIN, SONARR_SEARCH_MODE_BOTH, comment, SONARR_MANUAL_TYPE),
            )
        else:
            conn.execute(
                'INSERT INTO "SceneMappings"'
                ' ("Title", "ParseTerm", "SearchTerm", "TvdbId", "SeasonNumber",'
                '  "SceneSeasonNumber", "SceneOrigin", "SearchMode", "Comment",'
                '  "FilterRegex", "Type")'
                ' VALUES (?, ?, ?, ?, -1, NULL, ?, ?, ?, NULL, ?)',
                (title, parse_term, search_term, tvdb_id,
                 SONARR_MANUAL_ORIGIN, SONARR_SEARCH_MODE_BOTH, "Manual alias", SONARR_MANUAL_TYPE),
            )

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


def _add_radarr_alias(data):
    metadata_id = data.get("metadata_id")
    title = data.get("title", "").strip()

    if not metadata_id or not title:
        return jsonify({"error": "metadata_id and title are required"}), 400

    clean_title = clean_movie_title(title)
    if not clean_title:
        return jsonify({"error": "Title normalizes to an empty string"}), 400

    conn = get_radarr_db()
    if not conn:
        return jsonify({"error": "Radarr database not configured"}), 503

    with conn:
        _ensure_tagarr_table(conn)

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
        conn.execute(
            'INSERT OR IGNORE INTO "TagarrAliases"'
            ' ("Title", "CleanTitle", "MovieMetadataId")'
            ' VALUES (?, ?, ?)',
            (title, clean_title, metadata_id),
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
            'SELECT "Title", "Type", "Comment", "TvdbId" FROM "SceneMappings" WHERE "Id" = ?',
            (alias_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Alias not found"}), 404

        if row["Type"] != SONARR_MANUAL_TYPE:
            return jsonify({"error": "Only manual aliases can be removed"}), 403

        comment = row["Comment"] or ""
        if comment.startswith("network:"):
            conn.execute(
                'DELETE FROM "SceneMappings" WHERE "TvdbId" = ? AND "Type" = ? AND "Comment" = ?',
                (row["TvdbId"], SONARR_MANUAL_TYPE, comment),
            )
        else:
            conn.execute('DELETE FROM "SceneMappings" WHERE "Id" = ?', (alias_id,))

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return jsonify({"ok": True})


def _remove_radarr_alias(alias_id):
    conn = get_radarr_db()
    if not conn:
        return jsonify({"error": "Radarr database not configured"}), 503

    with conn:
        _ensure_tagarr_table(conn)

        row = conn.execute(
            'SELECT "Title", "CleanTitle", "MovieMetadataId", "SourceType"'
            ' FROM "AlternativeTitles" WHERE "Id" = ?',
            (alias_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Alias not found"}), 404

        if row["SourceType"] != RADARR_MANUAL_SOURCE_TYPE:
            return jsonify({"error": "Only manual aliases can be removed"}), 403

        conn.execute('DELETE FROM "AlternativeTitles" WHERE "Id" = ?', (alias_id,))
        conn.execute(
            'DELETE FROM "TagarrAliases"'
            ' WHERE "MovieMetadataId" = ? AND "CleanTitle" = ?',
            (row["MovieMetadataId"], row["CleanTitle"]),
        )

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
    app.run(host="0.0.0.0", port=5757)
