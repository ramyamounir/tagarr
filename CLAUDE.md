# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Tagarr is a web UI for managing manual scene name aliases in Sonarr and Radarr SQLite databases. It lets users search their Sonarr series library and Radarr movie library, then add/remove custom aliases that help each app match release names to the correct media. Only manually-added aliases can be deleted; auto-imported ones are read-only. The app works with either database alone or both together.

## Architecture

Single-file Flask app (`app.py`) with an inline HTML/JS frontend (`templates/index.html`). No ORM — raw SQLite queries against Sonarr's and Radarr's databases.

- **Backend**: `app.py` — Flask routes for search, add alias, remove alias. Connects to Sonarr's `sonarr.db` and Radarr's `radarr.db` via `sqlite3`. Each DB is optional — the app gracefully skips unconfigured or missing databases.
- **Frontend**: `templates/index.html` — self-contained SPA with inline CSS/JS. Vanilla JS, no build step. Cards show "TV" or "Movie" badges to distinguish media types.

**Sonarr DB**: `Series` (series metadata), `SceneMappings` (aliases). Manual aliases: `Type = "ManualMapping"` and `SceneOrigin = "manual"`.

**Radarr DB**: `Movies` → `MovieMetadata` (3-table join), `AlternativeTitles` (aliases). Manual aliases: `SourceType = 2`.

All endpoints use a `source` field (`"sonarr"` or `"radarr"`) to route to the correct database. Search returns merged results from both.

`clean_series_title()` mirrors Sonarr/Radarr's title normalization (strip articles, punctuation, diacritics) to generate `ParseTerm`/`CleanTitle` fields.

## Running

```bash
# Development
flask --app app run --debug          # http://localhost:5000

# Production (Docker)
docker compose up --build

# Environment
# SONARR_DB — path to sonarr.db (default: empty, skipped if not set)
# RADARR_DB — path to radarr.db (default: empty, skipped if not set)
```

## Dependencies

Runtime: `flask`, `gunicorn` (see `requirements.txt`). Python 3.13+ (`.python-version`). The Dockerfile uses 3.12-slim with pre-built wheels.

## API Endpoints

- `GET /` — serves the SPA
- `GET /health` — per-database health status, 200 if at least one DB is healthy
- `GET /api/search?q=<term>` — search series and movies by title, returns merged results with `source` and `media_type` fields
- `POST /api/alias` — add manual alias (`{source, tvdb_id|metadata_id, title, ...}`)
- `DELETE /api/alias/<id>?source=sonarr|radarr` — remove a manual alias (refuses to delete auto aliases)
