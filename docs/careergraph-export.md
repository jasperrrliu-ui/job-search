# CareerGraph Tracker export

The existing Job search workflow polls ATS sources and restores `data/jobs.db` from the GitHub Actions cache. The morning poll only updates the Tracker database. After the afternoon poll succeeds, this branch exports **all** jobs for active companies, including OPEN and CLOSED rows, and uploads one dated snapshot to GCS. The afternoon fallback skips the upload if that day's manifest already exists. It does not run CareerGraph parsing.

## Output contract

For each daily afternoon export, `job_tracker.export` writes:

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

The scheduled export runs after the afternoon poll once the workflow change is on the default branch. A manual **Job search** run still performs a Tracker poll and does not upload a CareerGraph snapshot. To verify this branch before deployment, inspect its workflow and export files; the first GCS snapshot requires the scheduled afternoon run after the branch is incorporated into the default branch.

Check the afternoon workflow log for `job_count`, company poll status, and three successful GCS uploads. Then check `gs://ms3_data_ingestion/careergraph/exports/latest.json` and the matching run folder. CareerGraph Cloud Run and Vertex processing are a separate deployment and are not triggered by this GitHub workflow.

Scheduled GitHub Actions workflows use the default branch. The existing morning and afternoon poll schedule remains unchanged; only the afternoon run exports to GCS after this branch's changes are incorporated into the default branch.
