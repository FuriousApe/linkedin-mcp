import logging
import re
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

_EMPLOYERS_FILE = Path(__file__).parent.parent / "data" / "lca_employers.txt"

_SUFFIX = re.compile(
    r"[\s,]+(LLC|INC|INCORPORATED|CORP|CORPORATION|LTD|LP|LLP|PLLC|PC|CO|PTY|PLC)\b\.?$",
    re.IGNORECASE,
)

_employers_list: list[str] = []
_employers_set: set[str] = set()


def load() -> None:
    global _employers_list, _employers_set
    if not _EMPLOYERS_FILE.exists():
        logger.warning("LCA employers file not found — H-1B sponsor checks disabled. Run src/build_lca.py first.")
        return
    names = [n for n in _EMPLOYERS_FILE.read_text(encoding="utf-8").splitlines() if n]
    _employers_list = names
    _employers_set = set(names)
    logger.info(f"LCA database loaded: {len(_employers_list):,} certified H-1B employers")


def _normalize(name: str) -> str:
    name = name.upper().strip()
    name = _SUFFIX.sub("", name).strip().rstrip(",").strip()
    return name


def is_known_sponsor(company: str, threshold: int = 90) -> Optional[bool]:
    """
    Returns True if company is a known H-1B LCA filer, False if not,
    None if the LCA database hasn't been loaded.
    """
    if not _employers_list:
        return None
    norm = _normalize(company)
    if not norm:
        return None
    if norm in _employers_set:
        return True
    match = process.extractOne(norm, _employers_list, scorer=fuzz.WRatio, score_cutoff=threshold)
    return match is not None
