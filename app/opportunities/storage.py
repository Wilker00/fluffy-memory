"""Persistence implementations for opportunity pipeline state."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from app.opportunities.models import (
    ApplicationPackage,
    CandidateProfile,
    Opportunity,
    OpportunityCase,
    PipelineEntry,
    SearchRequest,
    SubmissionRecord,
)
from app.sqlite_utils import connect_sqlite, initialize_sqlite_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_profiles (
    profile_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_requests (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    request_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_cases (
    case_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    case_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunity_cases_request
    ON opportunity_cases (request_id);
CREATE TABLE IF NOT EXISTS opportunity_catalog (
    opportunity_id TEXT PRIMARY KEY,
    opportunity_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_packages (
    package_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    package_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_submissions (
    submission_id TEXT PRIMARY KEY,
    submission_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_pipeline (
    case_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    entry_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunity_pipeline_profile
    ON opportunity_pipeline (profile_id);
CREATE TABLE IF NOT EXISTS opportunity_provider_keys (
    idempotency_key TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_provider_submissions (
    submission_id TEXT PRIMARY KEY,
    submission_json TEXT NOT NULL
);
"""


class OpportunityPersistence(Protocol):
    def put_profile(self, profile: CandidateProfile) -> CandidateProfile: ...

    def get_profile(self, profile_id: str) -> CandidateProfile | None: ...

    def save_search(
        self,
        *,
        request: SearchRequest,
        cases: list[OpportunityCase],
        opportunities: list[Opportunity],
        pipeline: list[PipelineEntry],
    ) -> None: ...

    def get_request(self, request_id: str) -> SearchRequest | None: ...

    def request_ids(self) -> tuple[str, ...]: ...

    def cases_for_request(self, request_id: str) -> list[OpportunityCase]: ...

    def get_case(self, case_id: str) -> OpportunityCase | None: ...

    def put_opportunity(self, opportunity: Opportunity) -> None: ...

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...

    def package_for_case(self, case_id: str) -> ApplicationPackage | None: ...

    def put_package(self, package: ApplicationPackage) -> None: ...

    def get_package(self, package_id: str) -> ApplicationPackage | None: ...

    def put_submission(self, record: SubmissionRecord) -> None: ...

    def get_submission(self, submission_id: str) -> SubmissionRecord | None: ...

    def put_pipeline(self, entry: PipelineEntry, *, profile_id: str) -> None: ...

    def get_pipeline(self, case_id: str) -> PipelineEntry | None: ...

    def pipeline_for_profile(self, profile_id: str) -> list[PipelineEntry]: ...

    def get_provider_submission(self, idempotency_key: str) -> SubmissionRecord | None: ...

    def get_provider_submission_by_id(self, submission_id: str) -> SubmissionRecord | None: ...

    def put_provider_submission(
        self, *, idempotency_key: str, record: SubmissionRecord
    ) -> None: ...

    def reset(self) -> None: ...

    def reset_provider(self) -> None: ...


class MemoryOpportunityPersistence:
    """Process-local implementation retained for isolated tests and demos."""

    def __init__(self) -> None:
        self._profiles: dict[str, CandidateProfile] = {}
        self._requests: dict[str, SearchRequest] = {}
        self._cases: dict[str, OpportunityCase] = {}
        self._opportunities: dict[str, Opportunity] = {}
        self._packages: dict[str, ApplicationPackage] = {}
        self._packages_by_case: dict[str, str] = {}
        self._submissions: dict[str, SubmissionRecord] = {}
        self._pipeline: dict[str, PipelineEntry] = {}
        self._pipeline_profile: dict[str, str] = {}
        self._provider_keys: dict[str, str] = {}
        self._provider_submissions: dict[str, SubmissionRecord] = {}

    def put_profile(self, profile: CandidateProfile) -> CandidateProfile:
        existing = self._profiles.get(profile.profile_id)
        if existing is not None and existing.tenant_id != profile.tenant_id:
            raise PermissionError("Profile identifier is already bound to another tenant.")
        self._profiles[profile.profile_id] = profile
        return profile

    def get_profile(self, profile_id: str) -> CandidateProfile | None:
        return self._profiles.get(profile_id)

    def save_search(
        self,
        *,
        request: SearchRequest,
        cases: list[OpportunityCase],
        opportunities: list[Opportunity],
        pipeline: list[PipelineEntry],
    ) -> None:
        self._requests[request.request_id] = request
        for opportunity in opportunities:
            self._opportunities[opportunity.opportunity_id] = opportunity
        for case in cases:
            self._cases[case.case_id] = case
        for entry in pipeline:
            self._pipeline[entry.case_id] = entry
            case = self._cases[entry.case_id]
            self._pipeline_profile[entry.case_id] = case.profile_id

    def get_request(self, request_id: str) -> SearchRequest | None:
        return self._requests.get(request_id)

    def request_ids(self) -> tuple[str, ...]:
        return tuple(self._requests)

    def cases_for_request(self, request_id: str) -> list[OpportunityCase]:
        cases = [case for case in self._cases.values() if case.request_id == request_id]
        return sorted(cases, key=lambda case: (-case.match.score, case.opportunity_id))

    def get_case(self, case_id: str) -> OpportunityCase | None:
        return self._cases.get(case_id)

    def put_opportunity(self, opportunity: Opportunity) -> None:
        self._opportunities[opportunity.opportunity_id] = opportunity

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        return self._opportunities.get(opportunity_id)

    def package_for_case(self, case_id: str) -> ApplicationPackage | None:
        package_id = self._packages_by_case.get(case_id)
        if package_id is None:
            return None
        return self._packages.get(package_id)

    def put_package(self, package: ApplicationPackage) -> None:
        self._packages[package.package_id] = package
        self._packages_by_case[package.case_id] = package.package_id

    def get_package(self, package_id: str) -> ApplicationPackage | None:
        return self._packages.get(package_id)

    def put_submission(self, record: SubmissionRecord) -> None:
        self._submissions[record.submission_id] = record

    def get_submission(self, submission_id: str) -> SubmissionRecord | None:
        return self._submissions.get(submission_id)

    def put_pipeline(self, entry: PipelineEntry, *, profile_id: str) -> None:
        self._pipeline[entry.case_id] = entry
        self._pipeline_profile[entry.case_id] = profile_id

    def get_pipeline(self, case_id: str) -> PipelineEntry | None:
        return self._pipeline.get(case_id)

    def pipeline_for_profile(self, profile_id: str) -> list[PipelineEntry]:
        case_ids = sorted(
            case_id
            for case_id, owner in self._pipeline_profile.items()
            if owner == profile_id
        )
        return [self._pipeline[case_id] for case_id in case_ids]

    def get_provider_submission(self, idempotency_key: str) -> SubmissionRecord | None:
        submission_id = self._provider_keys.get(idempotency_key)
        if submission_id is None:
            return None
        return self._provider_submissions.get(submission_id)

    def get_provider_submission_by_id(self, submission_id: str) -> SubmissionRecord | None:
        return self._provider_submissions.get(submission_id)

    def put_provider_submission(
        self, *, idempotency_key: str, record: SubmissionRecord
    ) -> None:
        self._provider_keys[idempotency_key] = record.submission_id
        self._provider_submissions[record.submission_id] = record

    def reset(self) -> None:
        self._profiles.clear()
        self._requests.clear()
        self._cases.clear()
        self._opportunities.clear()
        self._packages.clear()
        self._packages_by_case.clear()
        self._submissions.clear()
        self._pipeline.clear()
        self._pipeline_profile.clear()
        self.reset_provider()

    def reset_provider(self) -> None:
        self._provider_keys.clear()
        self._provider_submissions.clear()


class SQLiteOpportunityPersistence:
    """File-backed opportunity pipeline with tenant-bound profiles."""

    def __init__(self, db_url: str) -> None:
        self._database = initialize_sqlite_file(db_url)
        with connect_sqlite(self._database) as connection:
            connection.executescript(_SCHEMA)

    def put_profile(self, profile: CandidateProfile) -> CandidateProfile:
        try:
            with connect_sqlite(self._database) as connection:
                connection.execute(
                    """
                    INSERT INTO opportunity_profiles (profile_id, tenant_id, profile_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT (profile_id) DO UPDATE SET
                        profile_json = excluded.profile_json
                    WHERE opportunity_profiles.tenant_id = excluded.tenant_id
                    """,
                    (profile.profile_id, profile.tenant_id, profile.model_dump_json()),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PermissionError(
                        "Profile identifier is already bound to another tenant."
                    )
        except sqlite3.IntegrityError as exc:
            raise PermissionError(
                "Profile identifier is already bound to another tenant."
            ) from exc
        return profile

    def get_profile(self, profile_id: str) -> CandidateProfile | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT profile_json FROM opportunity_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return CandidateProfile.model_validate_json(row["profile_json"]) if row else None

    def save_search(
        self,
        *,
        request: SearchRequest,
        cases: list[OpportunityCase],
        opportunities: list[Opportunity],
        pipeline: list[PipelineEntry],
    ) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_requests
                    (request_id, tenant_id, profile_id, request_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (request_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    profile_id = excluded.profile_id,
                    request_json = excluded.request_json
                """,
                (
                    request.request_id,
                    request.tenant_id,
                    request.profile_id,
                    request.model_dump_json(),
                ),
            )
            for opportunity in opportunities:
                connection.execute(
                    """
                    INSERT INTO opportunity_catalog (opportunity_id, opportunity_json)
                    VALUES (?, ?)
                    ON CONFLICT (opportunity_id) DO UPDATE SET
                        opportunity_json = excluded.opportunity_json
                    """,
                    (opportunity.opportunity_id, opportunity.model_dump_json()),
                )
            for case in cases:
                connection.execute(
                    """
                    INSERT INTO opportunity_cases
                        (case_id, request_id, profile_id, opportunity_id, case_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (case_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        profile_id = excluded.profile_id,
                        opportunity_id = excluded.opportunity_id,
                        case_json = excluded.case_json
                    """,
                    (
                        case.case_id,
                        case.request_id,
                        case.profile_id,
                        case.opportunity_id,
                        case.model_dump_json(),
                    ),
                )
            for entry in pipeline:
                case = next(item for item in cases if item.case_id == entry.case_id)
                connection.execute(
                    """
                    INSERT INTO opportunity_pipeline (case_id, profile_id, entry_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT (case_id) DO UPDATE SET
                        profile_id = excluded.profile_id,
                        entry_json = excluded.entry_json
                    """,
                    (entry.case_id, case.profile_id, entry.model_dump_json()),
                )

    def get_request(self, request_id: str) -> SearchRequest | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT request_json FROM opportunity_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return SearchRequest.model_validate_json(row["request_json"]) if row else None

    def request_ids(self) -> tuple[str, ...]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                "SELECT request_id FROM opportunity_requests ORDER BY rowid"
            ).fetchall()
        return tuple(row["request_id"] for row in rows)

    def cases_for_request(self, request_id: str) -> list[OpportunityCase]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                """
                SELECT case_json FROM opportunity_cases
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
        cases = [OpportunityCase.model_validate_json(row["case_json"]) for row in rows]
        return sorted(cases, key=lambda case: (-case.match.score, case.opportunity_id))

    def get_case(self, case_id: str) -> OpportunityCase | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT case_json FROM opportunity_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return OpportunityCase.model_validate_json(row["case_json"]) if row else None

    def put_opportunity(self, opportunity: Opportunity) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_catalog (opportunity_id, opportunity_json)
                VALUES (?, ?)
                ON CONFLICT (opportunity_id) DO UPDATE SET
                    opportunity_json = excluded.opportunity_json
                """,
                (opportunity.opportunity_id, opportunity.model_dump_json()),
            )

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT opportunity_json FROM opportunity_catalog WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
        return Opportunity.model_validate_json(row["opportunity_json"]) if row else None

    def package_for_case(self, case_id: str) -> ApplicationPackage | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT package_json FROM opportunity_packages WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return ApplicationPackage.model_validate_json(row["package_json"]) if row else None

    def put_package(self, package: ApplicationPackage) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_packages (package_id, case_id, package_json)
                VALUES (?, ?, ?)
                ON CONFLICT (package_id) DO UPDATE SET
                    case_id = excluded.case_id,
                    package_json = excluded.package_json
                """,
                (package.package_id, package.case_id, package.model_dump_json()),
            )

    def get_package(self, package_id: str) -> ApplicationPackage | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT package_json FROM opportunity_packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
        return ApplicationPackage.model_validate_json(row["package_json"]) if row else None

    def put_submission(self, record: SubmissionRecord) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_submissions (submission_id, submission_json)
                VALUES (?, ?)
                ON CONFLICT (submission_id) DO UPDATE SET
                    submission_json = excluded.submission_json
                """,
                (record.submission_id, record.model_dump_json()),
            )

    def get_submission(self, submission_id: str) -> SubmissionRecord | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT submission_json FROM opportunity_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return SubmissionRecord.model_validate_json(row["submission_json"]) if row else None

    def put_pipeline(self, entry: PipelineEntry, *, profile_id: str) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_pipeline (case_id, profile_id, entry_json)
                VALUES (?, ?, ?)
                ON CONFLICT (case_id) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    entry_json = excluded.entry_json
                """,
                (entry.case_id, profile_id, entry.model_dump_json()),
            )

    def get_pipeline(self, case_id: str) -> PipelineEntry | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                "SELECT entry_json FROM opportunity_pipeline WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return PipelineEntry.model_validate_json(row["entry_json"]) if row else None

    def pipeline_for_profile(self, profile_id: str) -> list[PipelineEntry]:
        with connect_sqlite(self._database) as connection:
            rows = connection.execute(
                """
                SELECT case_id, entry_json FROM opportunity_pipeline
                WHERE profile_id = ?
                ORDER BY case_id
                """,
                (profile_id,),
            ).fetchall()
        return [PipelineEntry.model_validate_json(row["entry_json"]) for row in rows]

    def get_provider_submission(self, idempotency_key: str) -> SubmissionRecord | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                """
                SELECT s.submission_json
                FROM opportunity_provider_keys k
                JOIN opportunity_provider_submissions s
                    ON s.submission_id = k.submission_id
                WHERE k.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return SubmissionRecord.model_validate_json(row["submission_json"]) if row else None

    def get_provider_submission_by_id(self, submission_id: str) -> SubmissionRecord | None:
        with connect_sqlite(self._database) as connection:
            row = connection.execute(
                """
                SELECT submission_json FROM opportunity_provider_submissions
                WHERE submission_id = ?
                """,
                (submission_id,),
            ).fetchone()
        return SubmissionRecord.model_validate_json(row["submission_json"]) if row else None

    def put_provider_submission(
        self, *, idempotency_key: str, record: SubmissionRecord
    ) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_provider_submissions
                    (submission_id, submission_json)
                VALUES (?, ?)
                ON CONFLICT (submission_id) DO UPDATE SET
                    submission_json = excluded.submission_json
                """,
                (record.submission_id, record.model_dump_json()),
            )
            connection.execute(
                """
                INSERT INTO opportunity_provider_keys (idempotency_key, submission_id)
                VALUES (?, ?)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    submission_id = excluded.submission_id
                """,
                (idempotency_key, record.submission_id),
            )

    def reset(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.executescript(
                """
                DELETE FROM opportunity_profiles;
                DELETE FROM opportunity_requests;
                DELETE FROM opportunity_cases;
                DELETE FROM opportunity_catalog;
                DELETE FROM opportunity_packages;
                DELETE FROM opportunity_submissions;
                DELETE FROM opportunity_pipeline;
                DELETE FROM opportunity_provider_keys;
                DELETE FROM opportunity_provider_submissions;
                """
            )

    def reset_provider(self) -> None:
        with connect_sqlite(self._database) as connection:
            connection.execute("DELETE FROM opportunity_provider_keys")
            connection.execute("DELETE FROM opportunity_provider_submissions")


def build_opportunity_persistence(backend: str, db_url: str) -> OpportunityPersistence:
    if backend == "memory":
        return MemoryOpportunityPersistence()
    if backend == "sqlite":
        return SQLiteOpportunityPersistence(db_url)
    raise ValueError(f"Unsupported opportunity backend: {backend!r}")
