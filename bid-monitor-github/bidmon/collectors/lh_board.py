"""
LH 홈페이지 공모·공고 게시판 수집기
  https://www.lh.or.kr/board.es?mid=a10601020000&bid=0034
설계공모·민간참여사업 공모·위원 공고 등 전자조달(ebid)과 나라장터 어디에도 안 올라오는
홈페이지 전용 공고를 읽습니다. 목록 HTML 구조를 모르더라도 동작하도록
'list_no=' 링크 + 행 안의 날짜' 만으로 파싱합니다.
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

BASE = "https://www.lh.or.kr"
BOARD = BASE + "/board.es"
DEFAULT_BOARDS = [{"mid": "a10601020000", "bid": "0034", "name": "공모·공고"}]

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_LINK = re.compile(r"<a[^>]+href=\"([^\"]*list_no=(\d+)[^\"]*)\"[^>]*>(.*?)</a>", re.S)
_DATE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def _date(s: str) -> str:
    m = _DATE.search(s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


class LHBoardCollector(BaseCollector):
    name = "lh_board"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.boards = cfg.get("boards") or DEFAULT_BOARDS
        self.max_pages = int(cfg.get("max_pages", 5))
        self.min_days = int(cfg.get("days", 30))      # 증분 수집이어도 게시판은 항상 이 기간을 훑음 (가벼움)
        self.detail = bool(cfg.get("fetch_detail", True))
        self.known: set[str] = set()
        self.sess = _make_session()

    def _get(self, params: dict) -> str:
        last = ""
        for attempt in range(3):
            try:
                r = self.sess.get(BOARD, params=params, timeout=(15, 60))
                r.raise_for_status()
                if "charset" not in r.headers.get("content-type", ""):
                    r.encoding = r.apparent_encoding
                return r.text
            except requests.RequestException as e:
                last = str(e)
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"LH 게시판 3회 실패: {last}")

    def _parse_list(self, page: str, b: dict):
        """LH 홈페이지 게시판 행: 번호 | 본ㆍ지사 | 분류 | 제목(goView3) | 등록일 | 조회수 | 첨부"""
        for tr in _TR.findall(page):
            m = re.search(r"goView3\('(\d+)'\s*,\s*'([^']*)'\)[^>]*>(.*?)</a>", tr, re.S)
            if not m:
                m = re.search(r"<td[^>]*aria-label=\"제목\"[^>]*>.*?<a[^>]+href=\"([^\"]*list_no=(\d+)[^\"]*)\"[^>]*>(.*?)</a>", tr, re.S)
                if not m:
                    continue
                href, no, raw = m.group(1), m.group(2), m.group(3)
            else:
                no, href, raw = m.group(1), m.group(2), m.group(3)
            title = _text(raw)
            title = re.sub(r"^(새글|NEW|new|신규)\s*", "", title)
            title = re.sub(r"\s*(새글|NEW|new|N|신규)$", "", title).strip()
            if not title:
                continue
            url = _html.unescape(href)
            if url.startswith("/"):
                url = BASE + url
            elif not url.startswith("http"):
                url = f"{BOARD}?mid={b['mid']}&bid={b['bid']}&act=view&list_no={no}"
            def cell(label):
                c = re.search(r"<td[^>]*aria-label=\"" + label + r"\"[^>]*>(.*?)</td>", tr, re.S)
                return _text(c.group(1)) if c else ""
            date = _date(cell("등록일")) or _date(cell("작성일"))
            if not date:
                tds = [_text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                date = next((_date(c) for c in tds if _DATE.fullmatch(c.strip())), "")
            yield {"no": no, "title": title, "url": url, "date": date,
                   "kind": cell("분류"), "dept": cell("본ㆍ지사") or cell("본·지사")}

    def _detail(self, url: str) -> dict:
        out = {}
        try:
            r = self.sess.get(url, timeout=(15, 60))
            if "charset" not in r.headers.get("content-type", ""):
                r.encoding = r.apparent_encoding
            t = _text(r.text)
            # 접수/제출/마감 뒤에 나오는 기간의 끝 날짜
            m = re.search(r"(?:접수|제출|응모|신청|등록)\s*(?:기간|기한|마감)?[^0-9]{0,20}(20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2})[^~∼-]{0,30}[~∼-]\s*(20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2})(?:\D{0,6}(\d{1,2}:\d{2}))?", t)
            if m:
                out["close_date"] = _date(m.group(2).replace(" ", "")) + (" " + m.group(3) if m.group(3) else " 18:00")
            else:
                m = re.search(r"(?:마감|접수기한|제출기한)[^0-9]{0,15}(20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2})(?:\D{0,6}(\d{1,2}:\d{2}))?", t)
                if m:
                    out["close_date"] = _date(m.group(1).replace(" ", "")) + (" " + m.group(2) if m.group(2) else " 18:00")
            m = re.search(r"(?:등록일|작성일|게시일|공고일)[^0-9]{0,10}(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})", t)
            if m:
                out["notice_date"] = _date(m.group(1))
        except Exception as e:
            log.debug("lh_board 상세 실패 %s: %s", url, e)
        return out

    def collect(self, lookback_days: int) -> list[Notice]:
        cutoff = (datetime.now() - timedelta(days=max(lookback_days, self.min_days))).strftime("%Y-%m-%d")
        out: dict[str, Notice] = {}
        for b in self.boards:
            stop = False
            for p in range(1, self.max_pages + 1):
                page = self._get({"mid": b["mid"], "bid": b["bid"], "nPage": p})
                rows = list(self._parse_list(page, b))
                if not rows:
                    break
                for row in rows:
                    if row["date"] and row["date"] < cutoff:
                        stop = True
                        continue
                    key = f"lh_board|{row['no']}|00"
                    det = self._detail(row["url"]) if (self.detail and key not in self.known) else {}
                    if det:
                        time.sleep(0.3)
                    n = Notice(
                        source="lh_board", notice_no=row["no"], ord="00", title=row["title"],
                        institution="한국토지주택공사", demand_institution=(row.get("dept") or "") + " (홈페이지 " + b.get("name", "") + ")",
                        notice_date=(nd + " 00:00") if (nd := det.get("notice_date") or row["date"]) else "",
                        close_date=det.get("close_date", ""),
                        notice_kind=row.get("kind") or "공모·공고", url=row["url"],
                    )
                    out[n.key] = n
                log.info("lh_board %s p%d: %d건", b.get("name", b["bid"]), p, len(rows))
                if stop:
                    break
                time.sleep(0.5)
        return list(out.values())
