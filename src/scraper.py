import httpx
import asyncio
import json
import logging
import re
from urllib.parse import quote
from datetime import datetime, timezone
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .models import Job, JobSearchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LinkedIn Voyager API endpoints
# ---------------------------------------------------------------------------
SEARCH_ENDPOINT = "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
DETAIL_ENDPOINT = "https://www.linkedin.com/voyager/api/jobs/jobPostings/{job_id}"

# decorationId controls which fields LinkedIn returns — these are stable but
# can drift with LinkedIn releases. If fields go missing, check DevTools.
SEARCH_DECORATION = (
    "com.linkedin.voyager.dash.deco.jobs.nojuice.lite.JobPostingCardTerse-109"
)
DETAIL_DECORATION = (
    "com.linkedin.voyager.dash.deco.jobs.nojuice.JobPostingDetails-174"
)

# Maps days → seconds for LinkedIn's f_TPR / timePostedRange filter
DAYS_TO_SECONDS = {1: 86400, 2: 172800, 3: 259200, 7: 604800, 14: 1209600, 30: 2592000}

# LinkedIn geo URN for United States
GEO_US = "103644278"

# Workplace type filter codes
WORKPLACE_FILTERS = {
    "remote": "2",
    "hybrid": "3",
    "onsite": "1",
}


class LinkedInScraper:
    def __init__(self, li_at: str, jsessionid: str):
        """
        li_at       — your LinkedIn session cookie value
        jsessionid  — your JSESSIONID cookie value (used as CSRF token).
                      Strip surrounding quotes if present:
                      "ajax:1234..." → ajax:1234...
        """
        self.li_at = li_at
        self.jsessionid = jsessionid.strip('"')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def search(
        self,
        keywords: str,
        location: str = "United States",
        days_ago: int = 3,
        count: int = 25,
        remote_only: bool = False,
        geo_id: str = GEO_US,
    ) -> JobSearchResult:
        """
        Run a job search and return full details for each result.
        Calls the search endpoint to get job IDs, then the detail
        endpoint for each job to get the full description + skills.
        """
        raw_jobs = await self._search_jobs(keywords, location, days_ago, count, remote_only, geo_id)

        if not raw_jobs:
            return JobSearchResult(total_found=0, returned=0, jobs=[])

        # Fetch full details for each job concurrently (max 5 at a time)
        semaphore = asyncio.Semaphore(5)
        tasks = [self._fetch_with_semaphore(semaphore, raw) for raw in raw_jobs]
        jobs = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out failures
        valid_jobs = [j for j in jobs if isinstance(j, Job)]
        failed = len(jobs) - len(valid_jobs)
        if failed:
            logger.warning(f"{failed} job detail fetches failed")

        return JobSearchResult(
            total_found=len(raw_jobs),
            returned=len(valid_jobs),
            jobs=valid_jobs,
        )

    async def get_job_details(self, job_id_or_url: str) -> Job:
        """Fetch full details for a single job by ID or LinkedIn URL."""
        job_id = self._extract_job_id(job_id_or_url)
        raw = await self._fetch_job_detail(job_id)
        return self._parse_detail(job_id, raw)

    async def validate_cookie(self) -> dict:
        """Check if the li_at cookie is still valid."""
        try:
            async with self._client() as client:
                r = await client.get(
                    "https://www.linkedin.com/voyager/api/me",
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    profile = data.get("miniProfile", {})
                    name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
                    return {"valid": True, "account": name or "Unknown", "status": 200}
                elif r.status_code == 401:
                    return {"valid": False, "account": None, "status": 401, "reason": "Cookie expired or invalid"}
                else:
                    return {"valid": False, "account": None, "status": r.status_code, "reason": "Unexpected response"}
        except Exception as e:
            return {"valid": False, "account": None, "status": None, "reason": str(e)}

    def update_cookies(self, li_at: str, jsessionid: str):
        """Hot-reload cookies without restarting the container."""
        self.li_at = li_at
        self.jsessionid = jsessionid.strip('"')

    # ------------------------------------------------------------------
    # Private — HTTP client
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Build an httpx client with LinkedIn-realistic headers."""
        csrf = self.jsessionid  # JSESSIONID value = CSRF token

        headers = {
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
            "cookie": f"li_at={self.li_at}; JSESSIONID=\"{self.jsessionid}\"",
            "csrf-token": csrf,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "x-li-track": json.dumps({
                "clientVersion": "1.13.1965",
                "mpVersion": "1.13.1965",
                "osName": "web",
                "timezoneOffset": -5,
                "timezone": "America/New_York",
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
                "displayDensity": 1,
                "displayWidth": 1440,
                "displayHeight": 900,
            }),
            "referer": "https://www.linkedin.com/jobs/",
            "origin": "https://www.linkedin.com",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        return httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30)

    # ------------------------------------------------------------------
    # Private — Search
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    )
    async def _search_jobs(
        self, keywords, location, days_ago, count, remote_only, geo_id
    ) -> list[dict]:
        seconds = DAYS_TO_SECONDS.get(days_ago, days_ago * 86400)

        # Build the query string LinkedIn expects
        # This mirrors what the LinkedIn web app sends
        workplace_filter = f",workplaceTypes:List({WORKPLACE_FILTERS['remote']})" if remote_only else ""
        query = (
            f"(origin:JOB_SEARCH_RESULTS_PAGE,"
            f"keywords:{quote(keywords)},"
            f"locationUnion:(geoId:{geo_id}),"
            f"timePostedRange:(range:r{seconds})"
            f"{workplace_filter},"
            f"spellCorrectionEnabled:true)"
        )

        params = {
            "decorationId": SEARCH_DECORATION,
            "count": min(count, 50),  # LinkedIn caps at 49 per page
            "q": "jobSearch",
            "query": query,
            "start": 0,
        }

        async with self._client() as client:
            # Polite delay — don't hammer LinkedIn
            await asyncio.sleep(1.5)

            resp = await client.get(SEARCH_ENDPOINT, params=params)
            resp.raise_for_status()

            data = resp.json()
            elements = self._dig(data, "data", "elements") or []

            logger.debug(f"Search returned {len(elements)} raw elements")
            return elements

    # ------------------------------------------------------------------
    # Private — Job detail
    # ------------------------------------------------------------------

    async def _fetch_with_semaphore(self, sem: asyncio.Semaphore, raw: dict) -> Job:
        async with sem:
            await asyncio.sleep(0.8)  # polite spacing
            job_id = self._extract_job_id_from_element(raw)
            if not job_id:
                raise ValueError("Could not extract job ID from search element")
            detail_raw = await self._fetch_job_detail(job_id)
            return self._parse_detail(job_id, detail_raw, search_element=raw)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    )
    async def _fetch_job_detail(self, job_id: str) -> dict:
        url = DETAIL_ENDPOINT.format(job_id=job_id)
        params = {"decorationId": DETAIL_DECORATION}

        async with self._client() as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Private — Parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, job_id: str, raw: dict, search_element: dict = None) -> Job:
        """
        Parse a job detail API response into a Job model.

        LinkedIn's Voyager responses are deeply nested. We try multiple
        known paths for each field and fall back gracefully.
        """
        data = self._dig(raw, "data") or raw

        # --- Core fields ---
        title = (
            self._dig(data, "title")
            or self._dig(search_element, "jobPosting", "title")
            or "Unknown"
        )

        # Company — can be in several spots depending on decoration
        company = (
            self._dig(data, "companyDetails", "com.linkedin.voyager.deco.jobs.web.shared.WebJobPostingCompany", "companyResolutionResult", "name")
            or self._dig(data, "companyDetails", "company", "name")
            or self._dig(search_element, "jobPosting", "primaryDescription", "text")
            or "Unknown"
        )

        company_id = self._extract_company_id(data)

        location = (
            self._dig(data, "formattedLocation")
            or self._dig(search_element, "jobPosting", "secondaryDescription", "text")
            or "Unknown"
        )

        # --- Description ---
        description = (
            self._dig(data, "description", "text")
            or self._dig(data, "description")
            or ""
        )
        if isinstance(description, dict):
            description = description.get("text", "")

        # --- Skills ---
        skills_data = self._dig(data, "skills") or []
        skills = []
        if isinstance(skills_data, list):
            for s in skills_data:
                name = self._dig(s, "skill", "name") or self._dig(s, "name")
                if name:
                    skills.append(name)

        # --- Work metadata ---
        workplace_types = self._dig(data, "workplaceTypesResolutionResults") or {}
        workplace_type = None
        if isinstance(workplace_types, dict):
            for v in workplace_types.values():
                workplace_type = self._dig(v, "localizedName")
                break
        if not workplace_type:
            wt_list = self._dig(data, "workplaceTypes") or []
            workplace_type = wt_list[0] if wt_list else None

        employment_type = (
            self._dig(data, "employmentStatus")
            or self._dig(data, "employmentStatusResolutionResult", "localizedName")
        )

        seniority_level = (
            self._dig(data, "jobLevel")
            or self._dig(data, "jobLevelResolutionResult", "localizedName")
        )

        # --- Salary ---
        salary = self._parse_salary(data)

        # --- Applicants ---
        applicant_count = (
            self._dig(data, "applies")
            or self._dig(search_element, "jobPostingMetadata", "applicantsInsight", "numApplicantsText")
        )
        if isinstance(applicant_count, int):
            applicant_count = str(applicant_count)

        # --- Timestamps ---
        listed_at = (
            self._dig(data, "listedAt")
            or self._dig(search_element, "jobPosting", "listedAt")
        )
        posted_at = self._format_timestamp(listed_at)

        # --- Easy Apply ---
        easy_apply = bool(self._dig(data, "applyMethod", "com.linkedin.voyager.jobs.OffsiteApply") is None and
                          self._dig(data, "applyMethod"))

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
            applicant_count=str(applicant_count) if applicant_count else None,
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
        """Safely traverse nested dicts/lists. Returns None if any key is missing."""
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

    def _extract_job_id_from_element(self, element: dict) -> str | None:
        """Extract numeric job ID from a search result element."""
        # Try URN in entityUrn field: urn:li:fsd_jobPostingCard:(123456789,...)
        urn = self._dig(element, "entityUrn") or ""
        match = re.search(r"fsd_jobPostingCard:\((\d+)", urn)
        if match:
            return match.group(1)

        # Try from nested jobPosting
        job_urn = self._dig(element, "jobPosting", "entityUrn") or ""
        match = re.search(r"(\d+)$", job_urn)
        if match:
            return match.group(1)

        return None

    def _extract_job_id(self, id_or_url: str) -> str:
        """Accept a job ID or a LinkedIn job URL and return the numeric ID."""
        if id_or_url.isdigit():
            return id_or_url
        match = re.search(r"/jobs/view/(\d+)", id_or_url)
        if match:
            return match.group(1)
        match = re.search(r"currentJobId=(\d+)", id_or_url)
        if match:
            return match.group(1)
        # Last resort — find any long number
        match = re.search(r"(\d{8,})", id_or_url)
        if match:
            return match.group(1)
        raise ValueError(f"Cannot extract job ID from: {id_or_url}")

    def _extract_company_id(self, data: dict) -> str | None:
        company_urn = (
            self._dig(data, "companyDetails", "company")
            or self._dig(data, "companyDetails", "com.linkedin.voyager.deco.jobs.web.shared.WebJobPostingCompany", "companyResolutionResult", "entityUrn")
        )
        if company_urn and isinstance(company_urn, str):
            match = re.search(r"(\d+)$", company_urn)
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
            period = self._dig(comp, "payPeriod") or self._dig(comp, "payPeriod", "localizedName") or "YEARLY"
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
            # LinkedIn timestamps are in milliseconds
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return None
