# Deployment Notes

This project is prepared for Flask deployment on Render with either:

- local SQLite for development
- PostgreSQL for production through Supabase

## What is already configured

- `requirements.txt` for dependency install
- `gunicorn app:app` production start command
- `render.yaml` with:
  - web service config
  - environment variable placeholders
- PostgreSQL support through `DATABASE_URL`
- configurable SQLite database path via `STUDY_HALL_DB_PATH` when `DATABASE_URL` is not set
- `/healthz` route for health checks

## Required environment variables

- `STUDY_HALL_ENV`
- `DATABASE_URL`
- `STUDY_HALL_SECRET_KEY`
- `STUDY_HALL_ADMIN_USER`
- `STUDY_HALL_ADMIN_PASSWORD`
- `STUDY_HALL_DB_PATH` only if you want to override SQLite path locally

## Development and production databases

The app supports separate default database files for development and production when using SQLite:

- `development` -> local `database.db`
- `production` -> local `instance/production.db`

If `DATABASE_URL` is set, the app uses PostgreSQL and ignores SQLite for normal runtime.

The environment is controlled by `STUDY_HALL_ENV`.

Rules:

- if `DATABASE_URL` is set, PostgreSQL is used
- if `STUDY_HALL_DB_PATH` is set, that exact path is used
- otherwise, the app picks the default DB for the current `STUDY_HALL_ENV`

Examples:

- local development:
  - `STUDY_HALL_ENV=development`
  - uses `database.db`
- local production-style testing:
  - `STUDY_HALL_ENV=production`
  - uses `instance/production.db`
- Render + Supabase deployment:
  - `STUDY_HALL_ENV=production`
  - `DATABASE_URL=postgresql://...`

## Recommended Render setup

- set `STUDY_HALL_ENV=production`
- set `DATABASE_URL` to the Supabase Postgres connection string
- set real admin credentials in Render
- do not use default credentials in production

## Important note

For Supabase, use a pooled PostgreSQL connection string in `DATABASE_URL`.

After the first app start, login credentials can also be changed from inside the application. Environment credentials are mainly used as the initial bootstrap values for a fresh database.

If you need to move old local SQLite data into Supabase, run the migration script after creating the Supabase database:

```bash
python scripts/migrate_sqlite_to_postgres.py
```
