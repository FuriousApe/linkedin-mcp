import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from linkedin_api import Linkedin

from .models import Job, JobSearchResult

logger = logging.getLogger(__name__)

HOURS_TO_SECONDS = {1: 3600, 2: 7200, 6: 21600, 12: 43200, 24: 86400}
DAYS_TO_SECONDS = {1: 86400, 2: 172800, 3: 259200, 7: 604800, 14: 1209600, 30: 2592000}


class LinkedInScraper:
    def __init__(self, email: str, password: str):
        logger.info("Authenticating with LinkedIn...")
        self._api = Linkedin(email, password)
        self._executor = ThreadPoolExecutor(max_workers=3)
        logger.info("LinkedIn authentication successful")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def search(
        self,
        keywords: str,
        location: str = "United States",
        days_ago: int = 3,
        hours_ago: int = None,
        count: int = 25,
        remote_only: bool = False,
    ) -> JobSearchResult:
        if hours_ago is not None:
            listed_at = HOURS_TO_SECONDS.get(hours_ago, hours_ago * 3600)
        else:
            listed_at = DAYS_TO_SECONDS.get(days_ago, days_ago * 86400)

        raw_jobs = await self._run(
            self._api.search_jobs,
            keywords,
            location_name=location,
            remote=True if remote_only else None,
            listed_at=listed_at,
            limit=min(count, 49),
        )

        if not raw_jobs:
            return JobSearchResult(total_found=0, returned=0, jobs=[])

        jobs = []
        for card in raw_jobs:
            try:
                job_id = self._id_from_urn(card.get("entityUrn", ""))
                if not job_id:
                    continue
                detail = await self._run(self._api.get_job, job_id)
                jobs.append(self._parse(job_id, detail, card=card))
            except Exception as e:
                logger.warning(f"Skipping job — detail fetch failed: {e}")

        return JobSearchResult(total_found=len(raw_jobs), returned=len(jobs), jobs=jobs)

    async def get_job_details(self, job_id_or_url: str) -> Job:
        job_id = self._extract_job_id(job_id_or_url)
        detail = await self._run(self._api.get_job, job_id)
        return self._parse(job_id, detail)

    async def check_auth(self) -> dict:
        try:
            profile = await self._run(self._api.get_profile, "me")
            name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
            return {"authenticated": True, "account": name or "Unknown"}
        except Exception as e:
            return {"authenticated": False, "reason": str(e)}

    # ------------------------------------------------------------------
    # Private — async bridge
    # ------------------------------------------------------------------

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    # ------------------------------------------------------------------
    # Private — Parsing
    # ------------------------------------------------------------------

    def _parse(self, job_id: str, detail: dict, card: dict = None) -> Job:
        card = card or {}

        title = detail.get("title") or card.get("title") or "Unknown"

        company = (
            self._dig(detail, "companyDetails", "com.linkedin.voyager.deco.jobs.web.shared.WebJobPostingCompany", "companyResolutionResult", "name")
            or self._dig(detail, "companyDetails", "company", "name")
            or self._dig(card, "primaryDescription", "text")
            or "Unknown"
        )

        company_id = self._extract_company_id(detail)

        location = (
            detail.get("formattedLocation")
            or self._dig(card, "secondaryDescription", "text")
            or "Unknown"
        )

        desc = detail.get("description", {})
        description = desc.get("text", "") if isinstance(desc, dict) else str(desc or "")

        skills = [
            s["skill"]["name"]
            for s in (detail.get("skills") or [])
            if self._dig(s, "skill", "name")
        ]

        workplace_types = detail.get("workplaceTypesResolutionResults") or {}
        workplace_type = next(
            (self._dig(v, "localizedName") for v in workplace_types.values()), None
        ) if isinstance(workplace_types, dict) else None

        employment_type = (
            detail.get("employmentStatus")
            or self._dig(detail, "employmentStatusResolutionResult", "localizedName")
        )

        seniority_level = (
            detail.get("jobLevel")
            or self._dig(detail, "jobLevelResolutionResult", "localizedName")
        )

        salary = self._parse_salary(detail)

        applicant_count = detail.get("applies")
        if isinstance(applicant_count, int):
            applicant_count = str(applicant_count)

        listed_at = detail.get("listedAt") or card.get("listedAt")
        posted_at = self._format_timestamp(listed_at)

        easy_apply = bool(
            detail.get("applyMethod")
            and "com.linkedin.voyager.jobs.OffsiteApply" not in detail.get("applyMethod", {})
        )

        return Job(
            job_id=job_id,
            title=title,
            company=company,
            company_id=company_id,
            location=location,
            workplace_type=workplace_type,
            employment_type=employment_type,
            seniority_level=seniority_level,
            posted_at=posted_at,
            listed_at_timestamp=listed_at,
            applicant_count=applicant_count,
            salary=salary,
            description=description,
            skills=skills,
            linkedin_url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            easy_apply=easy_apply,
        )

    # ------------------------------------------------------------------
    # Private — Helpers
    # ------------------------------------------------------------------

    def _dig(self, obj, *keys):
        for key in keys:
            if obj is None:
                return None
            if isinstance(obj, dict):
                obj = obj.get(key)
            elif isinstance(obj, list) and isinstance(key, int):
                obj = obj[key] if key < len(obj) else None
            else:
                return None
        return obj

    def _id_from_urn(self, urn: str) -> str | None:
        match = re.search(r":(\d+)$", urn)
        return match.group(1) if match else None

    def _extract_job_id(self, id_or_url: str) -> str:
        if id_or_url.isdigit():
            return id_or_url
        for pattern in [r"/jobs/view/(\d+)", r"currentJobId=(\d+)", r"(\d{8,})"]:
            match = re.search(pattern, id_or_url)
            if match:
                return match.group(1)
        raise ValueError(f"Cannot extract job ID from: {id_or_url}")

    def _extract_company_id(self, data: dict) -> str | None:
        urn = (
            self._dig(data, "companyDetails", "company")
            or self._dig(data, "companyDetails", "com.linkedin.voyager.deco.jobs.web.shared.WebJobPostingCompany", "companyResolutionResult", "entityUrn")
        )
        if urn and isinstance(urn, str):
            match = re.search(r"(\d+)$", urn)
            if match:
                return match.group(1)
        return None

    def _parse_salary(self, data: dict) -> str | None:
        comp = self._dig(data, "compensationV2") or self._dig(data, "compensation")
        if not comp:
            return None
        try:
            min_val = self._dig(comp, "min", "value") or self._dig(comp, "minSalary", "amount")
            max_val = self._dig(comp, "max", "value") or self._dig(comp, "maxSalary", "amount")
            period = self._dig(comp, "payPeriod") or "YEARLY"
            currency = self._dig(comp, "currencyCode") or "USD"
            if min_val and max_val:
                return f"{currency} {int(min_val):,} – {int(max_val):,} / {period}"
        except Exception:
            pass
        return None

    def _format_timestamp(self, ts: int | None) -> str | None:
        if not ts:
            return None
        try:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return None
