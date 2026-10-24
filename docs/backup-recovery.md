# Backup and recovery runbook

This is an operational plan, not an enabled backup service. No backup schedule,
retention period, production database, or object-storage replication is configured
by the application. Choose those policies before onboarding production customers.

## Before backing up

1. Use a protected destination with access limited to the people who operate the
   platform. Database backups contain customer and account data; file backups can
   contain confidential construction imagery.
2. Record the application revision and database schema revision with each backup.
3. For a consistent snapshot, pause uploads and other writes while capturing the
   database and local file store. Resume only after both copies finish.
4. Keep encryption keys and application secrets in the approved secret manager,
   separately from the backup. Never put them in this repository or a backup log.

## Local development backup

The default local database is `instance/drone_portal.db`; local uploaded files are
in `static/uploads/`. Create a dated destination outside the application checkout,
then use SQLite's online backup command and copy the upload directory while the app
is paused:

```sh
mkdir -p /secure/construct-iv-backups/2026-10-20
sqlite3 instance/drone_portal.db \
  ".backup '/secure/construct-iv-backups/2026-10-20/drone_portal.db'"
ditto static/uploads /secure/construct-iv-backups/2026-10-20/uploads
```

Do not overwrite the source database or upload directory. Keep the backup outside
`static/`, which is served by the web application.

## Managed PostgreSQL backup

For a configured PostgreSQL environment, use the provider's managed backup or
`pg_dump` to a protected custom-format dump. Supply the database URL through the
operator's secure shell environment; do not paste credentials into a terminal
transcript or command checked into source control.

```sh
umask 077
pg_dump --format=custom --no-owner --file=/secure/construct-iv-backups/platform.dump "$DATABASE_URL"
```

Customer objects in Cloudflare R2 are separate from the relational database.
Database backups alone do not back up R2 objects. Choose and verify a bucket backup
or replication method with the storage provider before production use. Confirm its
cost and retention behavior first. R2 lifecycle rules that abort incomplete uploads
are cleanup, not backups.

## Restore rehearsal

Restore into an isolated, empty database and a separate file-store/bucket location.
Never test recovery by restoring over a live database or production bucket.

For SQLite, use a separate application checkout, copy the database backup to a new
path, and set `DATABASE_URL` to that copy for the recovery run. Restore the matching
upload backup into that checkout's `static/uploads/` directory before starting it.

For PostgreSQL, create an empty recovery database, then restore the custom dump.
If the restored database references R2 objects, configure an isolated recovery
bucket and credentials before starting the app. Restore or replicate the needed
objects there first; never point a recovery run at the live customer bucket:

```sh
pg_restore --no-owner --dbname "$RESTORE_DATABASE_URL" /secure/construct-iv-backups/platform.dump
```

After restoring, check that the application starts, schema migrations are current,
tenant counts and representative project records are present, file metadata points
to objects in the isolated store, and authorized playback/download works. Record
the rehearsal date, revision, duration, and any gaps. Do not expose the recovery
environment publicly.

## Production decisions still required

Before launch, the Platform Owner must select backup frequency, retention, recovery
time and data-loss objectives, encryption/key handling, off-site copies, and who may
restore data. Production database and R2 backup options can have provider costs;
the app currently reports tracked storage, not provider billing charges. Do not
delete customer data or apply a retention policy until the Owner has approved it.
