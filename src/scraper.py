import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests
from linkedin_api import Linkedin

from . import lca
from .models import Job, JobSearchResult

logger = logging.getLogger(__name__)

HOURS_TO_SECONDS = {1: 3600, 2: 7200, 6: 21600, 12: 43200, 24: 86400}
DAYS_TO_SECONDS = {1: 86400, 2: 172800, 3: 259200, 7: 604800, 14: 1209600, 30: 2592000}

_TITLE_BLOCK = re.compile(
    r"\b(ts[/ ]sci|secret\s+clearance|top[- ]?secret|position\s+cleared|clearance\s+required)\b",
    re.IGNORECASE,
)

# Patterns that explicitly disqualify a job for visa-sponsored candidates.
# Each tuple is (label, compiled_regex). A match on the lowercased description
# means the job is dropped when visa_filter=True.
_DISQUALIFY_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Sponsorship explicitly denied
    ("no_sponsorship", re.compile(
        r"will not sponsor|won'?t sponsor|unable to sponsor|cannot sponsor|"
        r"can'?t sponsor|does not sponsor|do not sponsor|don'?t sponsor|"
        r"no visa sponsorship|sponsorship is not (available|provided|offered)|"
        r"not (able|available) to sponsor|sponsorship will not|"
        r"not provid\w+ sponsorship|not offer\w+ sponsorship",
        re.IGNORECASE,
    )),
    # Security clearance required
    ("security_clearance", re.compile(
        r"(active\s+)?(secret|top\s*secret|ts\s*/\s*sci|sci)\s+(clearance|cleared)|"
        r"security clearance (is |are )?(required|mandatory|must)|"
        r"must (have|hold|possess|maintain) (an? )?(active\s+)?(secret|top secret|ts|sci|security) clearance|"
        r"requires?\s+(an?\s+)?(active\s+)?(secret|top secret|ts|sci|security) clearance|"
        r"clearance (is |are )?(required|mandatory)",
        re.IGNORECASE,
    )),
    # US citizenship required
    ("citizenship", re.compile(
        r"must be (a\s+)?u\.?s\.? citizen|"
        r"u\.?s\.? citizenship (is )?(required|mandatory|necessary)|"
        r"requires?\s+u\.?s\.? citizenship|"
        r"only u\.?s\.? citizens|citizen(s)? only|"
        r"limited to u\.?s\.? citizens",
        re.IGNORECASE,
    )),
    # Green card required (careful: "will sponsor green card" is positive — excluded)
    ("green_card", re.compile(
        r"(must|should) (be|have) (a\s+)?(permanent resident|green\s*card holder)|"
        r"green\s*card (is )?(required|mandatory|necessary)|"
        r"requires?\s+(a\s+)?green\s*card|"
        r"(permanent residency|green\s*card) (is )?(required|mandatory)",
        re.IGNORECASE,
    )),
]


class LinkedInScraper:
    def __init__(self, li_at: str, jsessionid: str):
        logger.info("Authenticating with LinkedIn...")
        cookie_jar = requests.cookies.RequestsCookieJar()
        cookie_jar.set("li_at", li_at)
        cookie_jar.set("JSESSIONID", jsessionid)
        self._api = Linkedin("", "", cookies=cookie_jar)
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
        hours_ago: Optional[int] = None,
        count: int = 25,
        remote_only: bool = False,
        visa_filter: bool = True,
    ) -> JobSearchResult:
        if hours_ago is not None:
            listed_at = HOURS_TO_SECONDS.get(hours_ago, hours_ago * 3600)
        else:
            listed_at = DAYS_TO_SECONDS.get(days_ago, days_ago * 86400)

        # --- Paginate cards, title-filter cheaply before fetching details ---
        cards: list[dict] = []
        page_size = 49
        # Fetch enough cards to fill count results after filtering losses
        card_target = min(count * 3, 150)
        offset = 0

        while len(cards) < card_target:
            page = await self._run(
                self._api.search_jobs,
                keywords,
                location_name=location,
                remote=True if remote_only else None,
                listed_at=listed_at,
                limit=page_size,
                offset=offset,
            )
            if not page:
                break
            for card in page:
                title = card.get("title") or ""
                if not _TITLE_BLOCK.search(title):
                    cards.append(card)
            if len(page) < page_size:
                break
            offset += page_size

        if not cards:
            return JobSearchResult(total_found=0, returned=0, jobs=[])

        total_found = len(cards)
        logger.info(f"Collected {total_found} cards after title filter ({offset + page_size} fetched)")

        # --- Fetch details for buffered set of cards ---
        detail_limit = min(len(cards), count + 20)
        jobs: list[Job] = []
        for card in cards[:detail_limit]:
            try:
                job_id = self._id_from_urn(card.get("entityUrn", ""))
                if not job_id:
                    continue
                detail = await self._run(self._api.get_job, job_id)
                job = self._parse(job_id, detail, card=card)
                job.lca_h1b_sponsor = lca.is_known_sponsor(job.company)
                jobs.append(job)
            except Exception as e:
                logger.warning(f"Skipping job — detail fetch failed: {e}")

        # --- Visa description filter ---
        if visa_filter:
            kept, dropped = [], []
            for job in jobs:
                (dropped if self._is_disqualified(job) else kept).append(job)
            if dropped:
                logger.info(f"Visa filter removed {len(dropped)} job(s): {[j.job_id for j in dropped]}")
            return JobSearchResult(
                total_found=total_found,
                returned=len(kept[:count]),
                sponsorship_filtered=len(dropped),
                jobs=kept[:count],
            )

        return JobSearchResult(total_found=total_found, returned=len(jobs[:count]), jobs=jobs[:count])

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

        company_info = self._resolve_company_details(detail.get("companyDetails") or {})
        company = (
            self._dig(company_info, "companyResolutionResult", "name")
            or self._dig(card, "primaryDescription", "text")
            or "Unknown"
        )

        company_id = self._extract_company_id(company_info)

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

    def _is_disqualified(self, job: Job) -> bool:
        text = (job.description or "") + " " + (job.title or "")
        for label, pattern in _DISQUALIFY_PATTERNS:
            if pattern.search(text):
                logger.info(f"Filtered [{label}]: {job.job_id} — {job.title} @ {job.company}")
                return True
        return False

    def _resolve_company_details(self, company_details: dict) -> dict:
        """Return the inner company data dict regardless of which class-name key LinkedIn uses."""
        for v in company_details.values():
            if isinstance(v, dict) and "companyResolutionResult" in v:
                return v
        return {}

    def _extract_company_id(self, company_info: dict) -> str | None:
        urn = company_info.get("company") or self._dig(company_info, "companyResolutionResult", "entityUrn")
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
