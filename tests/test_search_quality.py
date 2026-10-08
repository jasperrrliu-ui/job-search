import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.adapters import _target_title
from job_tracker.database import (
    connect,
    init_schema,
    reconcile_workday_discovery,
    record_poll,
    sync_companies,
    upsert_jobs,
)
from job_tracker.evaluation import (
    _candidate_gate,
    _career_stage,
    _experience_year_requirements,
    _freshness,
    _hard_eligibility,
    _qualification_paths,
)
from job_tracker.service import (
    build_source_health_report,
    build_tracker_status_report,
    evaluate_all_open_jobs,
)


ROOT = Path(__file__).resolve().parents[1]
DOMAIN = json.loads((ROOT / "config" / "domain.json").read_text(encoding="utf-8"))


class SearchQualityTests(unittest.TestCase):
    def test_all_data_career_titles_are_recalled(self):
        rules = DOMAIN["candidate_generation"]
        self.assertTrue(_target_title("Data Analyst I", rules))
        self.assertTrue(_target_title("Data Scientist", rules))
        self.assertTrue(_target_title("Junior Data Engineer", rules))
        self.assertTrue(_target_title("Machine Learning Engineer, New Grad", rules))
        self.assertTrue(_target_title("Biostatistician, Clinical Analytics", rules))
        self.assertTrue(_target_title("Decision Scientist", rules))
        self.assertTrue(_target_title("Senior Data Analyst", rules))
        self.assertFalse(_target_title("Quantitative Scientist", rules))
        self.assertFalse(_target_title("Software Engineer", rules))

    def test_four_primary_role_families_are_classified(self):
        cases = {
            "Data Analyst": "DATA_ANALYST",
            "Data Scientist": "DATA_SCIENTIST",
            "Data Engineer": "DATA_ENGINEER",
            "Machine Learning Engineer": "ML_ENGINEER",
        }
        for title, family in cases.items():
            with self.subTest(title=title):
                gate = _candidate_gate({"title": title, "description": ""}, DOMAIN, [])
                self.assertEqual(gate["status"], "PRIMARY")
                self.assertEqual(gate["family"], family)

    def test_internships_are_target_stage_not_employment_failure(self):
        job = {
            "title": "Data Analyst Intern",
            "location": "Boston, MA",
            "description": "Summer internship working with SQL and dashboards.",
        }
        paths = _qualification_paths(job["description"])
        eligibility, failures, _ = _hard_eligibility(job, DOMAIN, paths)
        self.assertEqual(eligibility, "UNKNOWN")
        self.assertNotIn("employment_type", {item["field"] for item in failures})
        self.assertEqual(_career_stage(job, paths, DOMAIN), "INTERNSHIP_COOP")

    def test_early_career_tracks_are_distinct(self):
        self.assertEqual(
            _career_stage(
                {"title": "Data Scientist, University Graduate", "description": ""},
                [],
                DOMAIN,
            ),
            "NEW_GRAD_CAMPUS",
        )
        self.assertEqual(
            _career_stage(
                {"title": "Data Engineer", "description": "Bachelor's degree and 2 years experience."},
                [{"degree": "BS", "years": 2}],
                DOMAIN,
            ),
            "FULL_TIME_0_2_YOE",
        )
        self.assertEqual(
            _career_stage(
                {"title": "Machine Learning Engineer", "description": ""},
                [],
                DOMAIN,
            ),
            "YOE_UNKNOWN",
        )

    def test_career_stage_parses_yoe_without_degree_phrase(self):
        self.assertEqual(
            _experience_year_requirements("Requires 0-2 years of relevant experience."),
            [0],
        )
        self.assertEqual(
            _career_stage(
                {"title": "Data Analyst", "description": "Requires 2+ years of experience."},
                [],
                DOMAIN,
            ),
            "FULL_TIME_0_2_YOE",
        )
        self.assertEqual(
            _career_stage(
                {"title": "Data Engineer", "description": "Requires 3-5 years of experience."},
                [],
                DOMAIN,
            ),
            "OUT_OF_SCOPE",
        )

    def test_internship_experience_does_not_make_a_job_an_internship(self):
        job = {
            "title": "Data Scientist",
            "description": "1 year of internship experience may count toward experience.",
        }
        self.assertEqual(_career_stage(job, [], DOMAIN), "FULL_TIME_0_2_YOE")

    def test_freshness_uses_source_dates_not_first_seen(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(
            _freshness({"official_created_at": "2026-09-29T00:00:00+00:00"}, now),
            "FRESH",
        )
        self.assertEqual(
            _freshness({"official_created_at": "2026-08-01T00:00:00+00:00"}, now),
            "STALE",
        )
        self.assertEqual(
            _freshness({"first_seen_at": "2026-09-30T00:00:00+00:00"}, now),
            "AGE_UNKNOWN",
        )

    def test_workday_listing_without_description_is_queued_for_enrichment(self):
        connection = connect(Path(":memory:"))
        init_schema(connection)
        company = {
            "name": "Example",
            "priority": "neutral",
            "active": True,
            "source": {"provider": "workday", "token": "example"},
        }
        sync_companies(connection, [company])
        listing = {
            "source_listing_id": "/job/123",
            "requisition_id": "/job/123",
            "title": "Data Scientist",
            "location": "Boston, MA",
            "official_url": "https://example.test/job/123",
            "description": "",
            "official_created_at": None,
            "official_updated_at": None,
        }
        upsert_jobs(connection, "Example", [listing], baseline=True, close_missing=False)
        self.assertEqual(
            reconcile_workday_discovery(connection, "Example", [listing]), [listing]
        )

    def test_source_health_exposes_current_failures(self):
        connection = connect(Path(":memory:"))
        init_schema(connection)
        companies = [
            {"name": "Healthy", "priority": "neutral", "active": True,
             "source": {"provider": "greenhouse", "token": "healthy"}},
            {"name": "Broken", "priority": "neutral", "active": True,
             "source": {"provider": "workday", "token": "broken"}},
        ]
        sync_companies(connection, companies)
        record_poll(connection, "Healthy", "2026-10-01T00:00:00+00:00", True, 2, None)
        record_poll(connection, "Broken", "2026-10-01T00:00:00+00:00", False, None, "HTTP 500")
        report = build_source_health_report(connection)
        self.assertIn("Latest poll failed: 1", report)
        self.assertIn("Broken (workday): HTTP 500", report)

    def test_reevaluate_does_not_require_a_poll(self):
        connection = connect(Path(":memory:"))
        init_schema(connection)
        company = {"name": "Example", "priority": "neutral", "active": True,
                   "source": {"provider": "greenhouse", "token": "example"}}
        sync_companies(connection, [company])
        upsert_jobs(connection, "Example", [{
            "requisition_id": "1", "title": "Data Scientist I", "location": "Boston, MA",
            "official_url": "https://example.test/1",
            "description": "Bachelor's degree and 1 years of data science experience.",
            "official_created_at": "2026-09-30T00:00:00+00:00", "official_updated_at": None,
        }], baseline=False)
        self.assertEqual(evaluate_all_open_jobs(connection, DOMAIN), 1)
        self.assertEqual(evaluate_all_open_jobs(connection, DOMAIN), 0)

    def test_status_report_shows_the_source_to_feedback_funnel(self):
        connection = connect(Path(":memory:"))
        init_schema(connection)
        company = {"name": "Example", "priority": "neutral", "active": True,
                   "source": {"provider": "greenhouse", "token": "example"}}
        sync_companies(connection, [company])
        record_poll(connection, "Example", "2026-10-01T00:00:00+00:00", True, 1, None)
        upsert_jobs(connection, "Example", [{
            "requisition_id": "1", "title": "Data Scientist", "location": "Boston, MA",
            "official_url": "https://example.test/1", "description": "Description",
            "official_created_at": None, "official_updated_at": None,
        }], baseline=False)
        evaluate_all_open_jobs(connection, DOMAIN)
        report = build_tracker_status_report(connection)
        self.assertIn("| 1. Sources | Registered companies | 1 |", report)
        self.assertIn("| 3. Inventory | Unique jobs retained | 1 |", report)
        self.assertIn("| 6. Outcomes | Feedback/application outcomes | 0 |", report)
        self.assertIn("| DATA_SCIENTIST | 1 |", report)
        self.assertIn("| YOE_UNKNOWN | 1 |", report)


if __name__ == "__main__":
    unittest.main()
