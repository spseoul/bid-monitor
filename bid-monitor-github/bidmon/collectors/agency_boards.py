"""
공사 홈페이지 게시판 수집기 — 나라장터에 안 올라오는 설계공모·사업자공모
  SH 서울주택도시개발공사  공고 및 공지 › 현상설계
     https://www.i-sh.co.kr/main/lay2/program/S1T294C302/www/brd/m_258/list.do?multi_itm_seq=64&page=N
  GH 경기주택도시공사      정보공개 › 입찰정보 › 공모 관련사항
     https://www.gh.or.kr/gh/bid-announcement.do?mode=list&article.offset=N&articleLimit=10
  IH 인천도시공사은 홈페이지 입찰공고가 나라장터 링크뿐이라 별도 수집 불필요.
"""
import logging
import json
import re
import time
from datetime import datetime, timedelta

import requests

from ..models import Notice
from .base import BaseCollector
from .lh import _make_session, _text
from .seoul_cleanup import SKIP
from ..attach import find_deadline, deadline_from_attachments

log = logging.getLogger(__name__)

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _d(s):
    """'2026-08-19' / '26.09.21' → 'YYYY-MM-DD'"""
    s = (s or "").strip()
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", s) or re.search(r"\b(\d{2})\.(\d{1,2})\.(\d{1,2})\b", s)
    if not m:
        return ""
    y = m.group(1)
    y = y if len(y) == 4 else "20" + y
    return f"{y}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


class _Board(BaseCollector):
    """공통: 페이지 순회, 날짜 컷오프, 채용/결과 공지 제외"""
    name = "board"
    inst = ""
    tag_hint = ""
    kind = "공모"
    skip_extra = re.compile(r"심사결과|결과\s*공지|결과공고|참가등록\s*결과|접수\s*결과|우선협상대상자|당첨자|심사위원\s*명단|업무중복도|수상작|선정\s*공지")

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.min_days = int(cfg.get("days", 90))   # 홈페이지 공모는 드물어 90일치 유지
        self.max_pages = int(cfg.get("max_pages", 10))
        self.known: set[str] = set()
        self.sess = _make_session()

    def _get(self, url, params=None):
        last = ""
        for attempt in range(3):
            try:
                r = self.sess.get(url, params=params, timeout=(15, 60))
                r.raise_for_status()
                if "charset" not in r.headers.get("content-type", ""):
                    r.encoding = r.apparent_encoding
                return r.text
            except requests.RequestException as e:
                last = str(e); time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"{self.name} 요청 3회 실패: {last}")

    def _rows(self, page_no: int):  # → iterable of dict(no,title,date,url,dept)
        raise NotImplementedError

    def _close(self, url: str) -> str:
        """상세 본문 → 첨부 공고문 순으로 마감일시 탐색 (첨부 목록은 self._last_files 에 남김)"""
        self._last_files = ""
        try:
            h = self._get(url)
            base = re.match(r"https?://[^/]+", url).group(0)
            files = []
            for m in re.finditer(r'<a[^>]*href="([^"]*(?:[Dd]ownload|[Ff]ile[Dd]own|attach|atchFile)[^"]*)"[^>]*>(.*?)</a>', h, re.S):
                u = m.group(1).replace("&amp;", "&")
                u = u if u.startswith("http") else base + (u if u.startswith("/") else "/" + u)
                nm = re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip()
                if u not in [f["url"] for f in files]:
                    files.append({"name": nm[:120] or f"첨부{len(files)+1}", "url": u})
            if files:
                self._last_files = json.dumps(files, ensure_ascii=False)
            dl = find_deadline(_text(h))
            if dl:
                return dl
            return deadline_from_attachments(self.sess, [f["url"] for f in files])
        except Exception as e:
            log.debug("%s 상세 실패 %s: %s", self.name, url, e)
            return ""

    def collect(self, lookback_days: int) -> list[Notice]:
        cutoff = (datetime.now() - timedelta(days=max(lookback_days, self.min_days))).strftime("%Y-%m-%d")
        out: dict[str, Notice] = {}
        for p in range(1, self.max_pages + 1):
            rows = list(self._rows(p))
            if not rows:
                break
            stop = False
            for r in rows:
                if r["date"] and r["date"] < cutoff:
                    stop = True
                    continue
                if SKIP.search(r["title"]) or self.skip_extra.search(r["title"]):
                    continue
                n = Notice(source=self.name, notice_no=r["no"], ord="00", title=r["title"],
                           institution=self.inst, demand_institution=r.get("dept", ""),
                           notice_date=(r["date"] + " 00:00") if r["date"] else "",
                           notice_kind=self.kind, url=r["url"])
                if n.key not in self.known:
                    n.close_date = self._close(r["url"])
                    n.files = getattr(self, "_last_files", "")
                    time.sleep(0.3)
                out[n.key] = n
            log.info("%s p%d: %d건 (누적 %d)", self.name, p, len(rows), len(out))
            if stop:
                break
            time.sleep(0.4)
        return list(out.values())


class SHBoardCollector(_Board):
    name = "sh_board"
    inst = "서울주택도시개발공사"
    kind = "현상설계"
    LIST = "https://www.i-sh.co.kr/main/lay2/program/S1T294C302/www/brd/m_258/list.do"
    VIEW = "https://www.i-sh.co.kr/main/lay2/program/S1T294C302/www/brd/m_258/view.do"

    def _rows(self, p):
        html = self._get(self.LIST, {"multi_itm_seq": "64", "page": p})
        for tr in _TR.findall(html):
            m = re.search(r"getDetailView\('(\d+)'\)", tr)
            if not m:
                continue
            tds = [_text(x) for x in _TD.findall(tr)]
            if len(tds) < 4:
                continue
            yield {"no": m.group(1), "title": tds[1], "dept": tds[2], "date": _d(tds[3]),
                   "url": f"{self.VIEW}?seq={m.group(1)}&multi_itm_seq=64"}


class GHBoardCollector(_Board):
    name = "gh_board"
    inst = "경기주택도시공사"
    kind = "공모"
    LIST = "https://www.gh.or.kr/gh/bid-announcement.do"

    def _rows(self, p):
        html = self._get(self.LIST, {"mode": "list", "article.offset": (p - 1) * 10, "articleLimit": 10})
        for tr in _TR.findall(html):
            m = re.search(r'<td class="title">.*?<a href="\?mode=view&(?:amp;)?articleNo=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S)
            if not m:
                continue
            dm = re.search(r'<td class="date">(.*?)</td>', tr, re.S)
            yield {"no": m.group(1), "title": _text(m.group(2)), "dept": "", "date": _d(_text(dm.group(1)) if dm else ""),
                   "url": f"{self.LIST}?mode=view&articleNo={m.group(1)}"}
