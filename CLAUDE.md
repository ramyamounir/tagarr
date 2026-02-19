# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Aliass is a web UI for managing manual scene name aliases in a Sonarr SQLite database. It lets users search their Sonarr series library and add/remove custom "scene mappings" (aliases) that help Sonarr match release names to the correct series. Only manually-added aliases can be deleted; auto-imported ones are read-only.

## Architecture

Single-file Flask app (`app.py`) with an inline HTML/JS frontend (`templates/index.html`). No ORM — raw SQLite queries against Sonarr's database.

- **Backend**: `app.py` — Flask routes for search, add alias, remove alias. Connects to Sonarr's `sonarr.db` via `sqlite3`.
- **Frontend**: `templates/index.html` — self-contained SPA with inline CSS/JS. Vanilla JS, no build step.
- **`main.py`**: Placeholder uv entrypoint, not used by the app.

Key tables in Sonarr's DB: `Series` (series metadata), `SceneMappings` (aliases). Manual aliases are identified by `Type = "ManualMapping"` and `SceneOrigin = "manual"`.

`clean_series_title()` mirrors Sonarr's own title normalization (strip articles, punctuation, diacritics) to generate the `ParseTerm` field.

## Running

```bash
# Development
flask --app app run --debug          # http://localhost:5000

# Production (Docker)
docker compose up --build

# Environment
# SONARR_DB — path to sonarr.db (default: /data/sonarr.db)
```

## Dependencies

Runtime: `flask`, `gunicorn` (see `requirements.txt`). Python 3.13+ (`.python-version`). The Dockerfile uses 3.12-slim with pre-built wheels.

## API Endpoints

- `GET /` — serves the SPA
- `GET /api/search?q=<term>` — search series by title/clean title, returns series with their aliases
- `POST /api/alias` — add manual alias (`{tvdb_id, title, search_term?, season?}`)
- `DELETE /api/alias/<id>` — remove a manual alias (refuses to delete auto aliases)
