from pydantic import BaseModel
from typing import Optional


class Job(BaseModel):
    job_id: str
    title: str
    company: str
    company_id: Optional[str] = None
    location: str
    workplace_type: Optional[str] = None   # REMOTE, HYBRID, ON_SITE
    employment_type: Optional[str] = None  # FULL_TIME, CONTRACT, etc.
    seniority_level: Optional[str] = None
    posted_at: Optional[str] = None
    listed_at_timestamp: Optional[int] = None
    applicant_count: Optional[str] = None
    salary: Optional[str] = None
    description: Optional[str] = None
    skills: list[str] = []
    linkedin_url: str
    easy_apply: bool = False


class JobSearchResult(BaseModel):
    total_found: int
    returned: int
    jobs: list[Job]
