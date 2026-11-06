# CareerGraph Tracker export

The existing Job search workflow polls ATS sources and restores `data/jobs.db` from the GitHub Actions cache. After a poll, this branch exports **all** jobs for active companies, including OPEN and CLOSED rows. It uploads the raw snapshot to GCS. It does not run CareerGraph parsing.

## Output contract

For each GitHub Actions run, `job_tracker.export` writes:

- `jobs.jsonl.gz`: full Tracker rows, one JSON object per job.
- `manifest.json`: run ID, UTC export time, row count, compressed-file SHA-256, per-company latest poll status, and poll details.
- `latest.json`: pointer to that run ID.

The workflow uploads these under `gs://ms3_data_ingestion/careergraph/exports/`. It uploads the data and manifest before updating `exports/latest.json`. CareerGraph's `DATA_STORE` must be `gs://ms3_data_ingestion/careergraph` so its importer can resolve the manifest's `jobs_key`.

If the latest poll for a company failed, its raw job rows remain in the export and the manifest marks the company `failed`. CareerGraph's importer carries forward previously published jobs for that company instead of inferring closures. If the latest poll succeeded, the manifest marks it `success`. `polls` also records each company's latest poll timestamp; a successful poll may have happened before the current workflow run when that company was not due for polling.

## One-time GCP access

The workflow uses GitHub OIDC through Google Workload Identity Federation. No service account key file is needed. In project `ms3-run-513900`:

1. Create a dedicated service account for Tracker export, for example `tracker-export@ms3-run-513900.iam.gserviceaccount.com`.
2. Grant it **Storage Object User** on the `ms3_data_ingestion` bucket. The workflow creates immutable run objects and updates `exports/latest.json`.
3. Create a Workload Identity Pool and an OIDC provider with issuer `https://token.actions.githubusercontent.com`. Map `google.subject` to `assertion.sub` and `attribute.repository` to `assertion.repository`. Restrict the provider to `assertion.repository == 'jasperrrliu-ui/job-search'`.
4. Grant the repository principal from that pool **Workload Identity User** on the Tracker export service account.
5. In this GitHub repository's **Settings → Secrets and variables → Actions → Variables**, set `GCP_WIF_PROVIDER` to the provider resource name and `GCP_TRACKER_EXPORT_SA` to the service account email. Both are identifiers, not secrets.

## First live run

Use **Actions → Job search → Run workflow**, selecting `ms3-careergraph-export` and leaving `send_digest` off. The workflow restores the cached database if available, polls, saves the updated cache, exports all rows, and uploads them. If the cache is unavailable, it calls Tracker `bootstrap` and the resulting database becomes the first snapshot.

Check the workflow log for `job_count`, company poll status, and three successful GCS uploads. Then check `gs://ms3_data_ingestion/careergraph/exports/latest.json` and the matching run folder. CareerGraph Cloud Run and Vertex processing are a separate deployment and are not triggered by this GitHub workflow.

Scheduled GitHub Actions workflows use the default branch. The export therefore runs on the existing schedule only after this branch's workflow changes are incorporated into the default branch.
