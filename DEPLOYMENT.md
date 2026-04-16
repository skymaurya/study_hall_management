# Deployment Notes

This project is prepared for simple Flask deployment on a platform like Render.

## What is already configured

- `requirements.txt` for dependency install
- `gunicorn app:app` production start command
- `render.yaml` with:
  - web service config
  - persistent disk mount
  - environment variable placeholders
- configurable SQLite database path via `STUDY_HALL_DB_PATH`
- `/healthz` route for health checks

## Required environment variables

- `STUDY_HALL_ENV`
- `STUDY_HALL_SECRET_KEY`
- `STUDY_HALL_ADMIN_USER`
- `STUDY_HALL_ADMIN_PASSWORD`
- `STUDY_HALL_DB_PATH`

## Development and production databases

The app now supports separate default database files for development and production:

- `development` -> local `database.db`
- `production` -> local `instance/production.db`

The environment is controlled by `STUDY_HALL_ENV`.

Rules:

- if `STUDY_HALL_DB_PATH` is set, that exact path is used
- otherwise, the app picks the default DB for the current `STUDY_HALL_ENV`

Examples:

- local development:
  - `STUDY_HALL_ENV=development`
  - uses `database.db`
- local production-style testing:
  - `STUDY_HALL_ENV=production`
  - uses `instance/production.db`
- Render deployment:
  - `STUDY_HALL_ENV=production`
  - `STUDY_HALL_DB_PATH=/var/data/database.db`

## Recommended Render setup

- attach a persistent disk
- set `STUDY_HALL_ENV=production`
- keep `STUDY_HALL_DB_PATH=/var/data/database.db`
- set real admin credentials in Render
- do not use default credentials in production

## Important note

This deployment setup keeps using SQLite, but stores the database file on persistent disk.

That is the fastest path to deploy this exact codebase.

After the first app start, login credentials can also be changed from inside the application. Environment credentials are mainly used as the initial bootstrap values for a fresh database.

If you later want a stronger multi-device / hosted-database setup, the next upgrade would be moving from SQLite to PostgreSQL.
