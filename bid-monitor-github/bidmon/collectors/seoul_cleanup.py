"""
서울시 정비사업 정보몽땅 — 조합입찰공고 수집기
  https://cleanup.seoul.go.kr/cleanup/bidpblanc/lscr.do?cpage=N  (10건/페이지, 공고일 내림차순)
재개발·재건축 조합이 발주하는 정비계획·건축설계·감정평가 등 용역 입찰이 모이는 곳.
나라장터에는 올라오지 않는다.
"""
import html as _html
import logging
import re
import time
from datetime import datetime, timedelta

import requests

from ..models import Notice
from .base import BaseCollector
from .lh import _make_session, _text

log = logging.getLogger(__name__)

BASE = "https://cleanup.seoul.go.kr"
LIST = BASE + "/cleanup/bidpblanc/lscr.do"

_LI = re.compile(r'<li>\s*<a href="(?P<href>/cleanup/bidpblanc/vscr\.do[^"]*othbcListSn=(?P<no>\d+)[^"]*)">(?P<body>.*?)</a>\s*</li>', re.S)
_H3 = re.compile(r'<h3[^>]*>(.*?)</h3>', re.S)
_FIELD = lambda label, body: (lambda m: _text(m.group(1)) if m else "")(re.search(label + r"\s*[:：]\s*(.*?)</span>", body, re.S))
_DATE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")
_TIME = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*(?:시|:)\s*(\d{2})?")

# 채용·총회 등 용역이 아닌 공고는 걸러냄 (제목 기준)
SKIP = re.compile(r"채용|총회|임시직|직원|선거|대의원|알림|안내문$")


def _date(s):
    m = _DATE.search(s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _close(s):
    """'2026-10-21 오전 10시' → '2026-10-21 10:00'"""
    d = _date(s)
    if not d:
        return ""
    rest = s[_DATE.search(s).end():]
    m = _TIME.search(rest)
    if m:
        h = int(m.group(2)); mi = int(m.group(3) or 0)
        if m.group(1) == "오후" and h < 12:
            h += 12
        return f"{d} {h:02d}:{mi:02d}"
    return d + " 18:00"


class SeoulCleanupCollector(BaseCollector):
    name = "seoul_cleanup"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.min_days = int(cfg.get("days", 30))
        self.max_pages = int(cfg.get("max_pages", 40))
        self.skip_nonservice = bool(cfg.get("skip_nonservice", True))
        self.known: set[str] = set()
        self.sess = _make_session()

    def _page(self, p: int) -> str:
        last = ""
        for attempt in range(3):
            try:
                r = self.sess.get(LIST, params={"cpage": p, "order": "occrrncDe desc"}, timeout=(15, 60))
                r.raise_for_status()
                if "charset" not in r.headers.get("content-type", ""):
                    r.encoding = r.apparent_encoding
                return r.text
            except requests.RequestException as e:
                last = str(e); time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"정보몽땅 목록 3회 실패: {last}")

    def collect(self, lookback_days: int) -> list[Notice]:
        cutoff = (datetime.now() - timedelta(days=max(lookback_days, self.min_days))).strftime("%Y-%m-%d")
        out: dict[str, Notice] = {}
        for p in range(1, self.max_pages + 1):
            page = self._page(p)
            items = list(_LI.finditer(page))
            if not items:
                break
            stop = False
            for m in items:
                body = m.group("body")
                h3 = _H3.search(body)
                title = _text(h3.group(1)) if h3 else ""
                nd = _date(_FIELD("공고일", body)) or _date(_FIELD("등록일", body))
                if nd and nd < cutoff:
                    stop = True
                    continue
                if not title or (self.skip_nonservice and SKIP.search(title)):
                    continue
                site = _FIELD("사업장명", body)
                out[f"seoul_cleanup|{m.group('no')}|00"] = Notice(
                    source="seoul_cleanup", notice_no=m.group("no"), ord="00", title=title,
                    institution=site or "서울 정비사업조합", demand_institution="서울 정비사업 정보몽땅",
                    notice_date=(nd + " 00:00") if nd else "",
                    close_date=_close(_FIELD("입찰마감일", body)),
                    notice_kind="조합입찰", url=BASE + _html.unescape(m.group("href")),
                )
            log.info("seoul_cleanup p%d: %d건 (누적 %d)", p, len(items), len(out))
            if stop:
                break
            time.sleep(0.4)
        return list(out.values())
