"""
부산광역시 정비사업 통합홈페이지 — 조합입찰공고 수집기
  https://dynamice.busan.go.kr/view.do?no=287&pageIndex=N  (10건/페이지, 등록일 내림차순)
각 글은 사업장(조합) 게시판으로 연결: /home/<BARA_id>/view.do?no=329&pgMode=view&ntt_id=<id>
"""
import logging
import re
import time
from datetime import datetime, timedelta

import requests

from ..models import Notice
from .base import BaseCollector
from .lh import _make_session, _text
from .seoul_cleanup import SKIP, _date, _close

log = logging.getLogger(__name__)

BASE = "https://dynamice.busan.go.kr"
LIST = BASE + "/view.do"

_LI = re.compile(
    r"move_view\('(?P<id>\d+)',\s*'(?P<bbs>[^']*)',\s*'(?P<area>[^']*)'\)\"?\s*>(?P<body>.*?)</a>", re.S)
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_REG = re.compile(r"등록일\s*[:：]\s*([\d.\-/]+)")


class BusanDynamiceCollector(BaseCollector):
    name = "busan_dynamice"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.min_days = int(cfg.get("days", 30))
        self.max_pages = int(cfg.get("max_pages", 20))
        self.skip_nonservice = bool(cfg.get("skip_nonservice", True))
        self.detail = bool(cfg.get("fetch_detail", True))
        self.known: set[str] = set()
        self.sess = _make_session()

    def _get(self, url, params):
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
        raise RuntimeError(f"부산 정비사업 요청 3회 실패: {last}")

    def _detail(self, url: str) -> dict:
        """사업장명(상단 로고 텍스트)과 입찰마감일(본문)을 상세에서 보조 추출"""
        out = {}
        try:
            h = self._get(url, None)
            m = re.search(r'class="gnb-logo".*?<span class="txt">(.*?)</span>', h, re.S)
            if m:
                out["site"] = _text(m.group(1))[:40]
            c = re.search(r'class="board-view-contants"[^>]*>(.*?)<!-- board-view-contants|class="board-view-contants"[^>]*>(.*)', h, re.S)
            body = _text(c.group(1) or c.group(2)) if c else _text(h)
            t = re.sub(r"\s+", "", body)   # 한글 문서 변환 시 글자 사이 공백이 끼어 들어가므로 제거
            dt = r"(20\d{2})[.\-/년](\d{1,2})[.\-/월](\d{1,2})일?"
            tm = r"\.?(?:\([가-힣]\))?(?:(오전|오후)?(\d{1,2})[:시](\d{2})?분?)?"
            for kw in (r"(?:입찰|제안서|응찰|참가신청|신청서|서류|등록)?(?:접수|제출)?마감", r"(?:접수|제출)(?:기한|기간|일시)", r"입찰(?:일시|기한)"):
                m = re.search(kw + r"[^0-9]{0,15}(?:" + dt + r"[^~∼-]{0,25}[~∼-])?" + dt + tm, t)
                if m:
                    g = m.groups()
                    y, mo, d = g[3], g[4], g[5]
                    ap, hh, mi = g[6], g[7], g[8]
                    hh = int(hh) if hh else 18
                    if ap == "오후" and hh < 12:
                        hh += 12
                    out["close"] = f"{y}-{int(mo):02d}-{int(d):02d} {hh:02d}:{int(mi or 0):02d}"
                    break
        except Exception as e:
            log.debug("busan 상세 실패 %s: %s", url, e)
        return out

    def collect(self, lookback_days: int) -> list[Notice]:
        cutoff = (datetime.now() - timedelta(days=max(lookback_days, self.min_days))).strftime("%Y-%m-%d")
        out: dict[str, Notice] = {}
        for p in range(1, self.max_pages + 1):
            page = self._get(LIST, {"no": "287", "pageIndex": p})
            items = list(_LI.finditer(page))
            if not items:
                break
            stop = False
            for m in items:
                body = m.group("body")
                h3 = _H3.search(body)
                title = re.sub(r"\s*N$", "", _text(h3.group(1))).strip() if h3 else ""
                rm = _REG.search(_text(body))
                nd = _date(rm.group(1)) if rm else ""
                if nd and nd < cutoff:
                    stop = True
                    continue
                if not title or (self.skip_nonservice and SKIP.search(title)):
                    continue
                key = f"busan_dynamice|{m.group('id')}|00"
                url = f"{BASE}/home/{m.group('area')}/view.do?no=329&pgMode=view&ntt_id={m.group('id')}"
                det = self._detail(url) if (self.detail and key not in self.known) else {}
                if det:
                    time.sleep(0.3)
                out[key] = Notice(
                    source="busan_dynamice", notice_no=m.group("id"), ord="00", title=title,
                    institution=det.get("site") or "부산 정비사업조합", demand_institution="부산 정비사업 통합홈페이지",
                    notice_date=(nd + " 00:00") if nd else "",
                    close_date=det.get("close", ""),
                    notice_kind="조합입찰", url=url,
                )
            log.info("busan_dynamice p%d: %d건 (누적 %d)", p, len(items), len(out))
            if stop:
                break
            time.sleep(0.4)
        return list(out.values())
