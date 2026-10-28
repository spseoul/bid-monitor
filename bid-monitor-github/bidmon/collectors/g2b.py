"""
나라장터(조달청) 입찰공고정보서비스 OpenAPI 수집기 - 용역(서비스) 공고

API: 조달청_나라장터 입찰공고정보서비스 (data.go.kr) / 오퍼레이션 getBidPblancListInfoServcPPSSrch
- inqryDiv=1 : 공고게시일시 기준 조회
- inqryBgnDt/inqryEndDt : YYYYMMDDHHMM (최대 1개월 범위 권장)
- type=json
LH·GH 등 자체조달시스템 공고도 나라장터에 연계 게시되므로 이 소스 하나로 대부분 커버됩니다.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from ..models import Notice
from .base import BaseCollector

log = logging.getLogger(__name__)


def _mask(msg: str) -> str:
    """오류 메시지/URL 에 섞인 인증키 제거 (로그 노출 방지)"""
    return re.sub(r"serviceKey=[^&\s'\")]*", "serviceKey=***", str(msg))


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None


def _fmt_dt(v: str) -> str:
    """'2026-09-01 10:00:00' / '202609011000' -> '2026-09-01 10:00'"""
    if not v:
        return ""
    v = str(v).strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, f).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return v[:16]


class G2BCollector(BaseCollector):
    name = "g2b"

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"]
        self.key = cfg.get("service_key", "")
        self.num_rows = int(cfg.get("num_rows", 500))
        self.inst_filter = cfg.get("institution_filter") or []
        if not self.key:
            raise RuntimeError("G2B_SERVICE_KEY 가 .env 에 없습니다.")

    # ── HTTP ──
    def _page(self, bgn: datetime, end: datetime, page: int, inst: str | None) -> dict:
        params = {
            "serviceKey": self.key,
            "type": "json",
            "inqryDiv": "1",
            "inqryBgnDt": bgn.strftime("%Y%m%d%H%M"),
            "inqryEndDt": end.strftime("%Y%m%d%H%M"),
            "pageNo": page,
            "numOfRows": self.num_rows,
        }
        if inst:
            params["ntceInsttNm"] = inst
        last_err = ""
        # 해외(GitHub) 에서 조달청 API 접속이 간헐적으로 막히므로: 6회 재시도, https↔http 번갈아 시도
        endpoints = [self.endpoint]
        alt = self.endpoint.replace("https://", "http://") if self.endpoint.startswith("https://") else self.endpoint.replace("http://", "https://")
        endpoints.append(alt)
        for attempt in range(6):
            ep = endpoints[attempt % 2]
            try:
                r = requests.get(ep, params=params, timeout=(20, 90))
                text = r.text
                if r.status_code != 200 or "<" in text[:5]:
                    snippet = text.strip().replace("\n", " ")[:300]
                    if "SERVICE_KEY" in text or "SERVICE KEY" in text or "인증" in text:
                        raise RuntimeError(f"인증키 오류 (G2B_SERVICE_KEY 확인): {snippet}")
                    raise RuntimeError(f"HTTP {r.status_code}: {snippet}")
                data = r.json()
                break
            except (requests.RequestException, ValueError, RuntimeError) as e:
                last_err = _mask(e)[:200]
                log.warning("g2b 요청 실패(%d/6, %s): %s", attempt + 1, ep.split("/")[2] + ("/https" if ep.startswith("https") else "/http"), last_err)
                time.sleep(min(10 * (attempt + 1), 45))
        else:
            raise RuntimeError(f"g2b API 6회 실패 — 마지막 오류: {last_err}")

        resp = data.get("response", {})
        header = resp.get("header", {})
        if str(header.get("resultCode")) not in ("00", "0", "None"):
            raise RuntimeError(f"g2b API 오류: {header}")
        return resp.get("body", {})

    def _iter_items(self, bgn: datetime, end: datetime, inst: str | None):
        page = 1
        while True:
            body = self._page(bgn, end, page, inst)
            items = body.get("items") or []
            if isinstance(items, dict):  # {"item": [...]} 형태 대비
                items = items.get("item") or []
            if isinstance(items, dict):
                items = [items]
            yield from items
            total = _to_int(body.get("totalCount")) or 0
            log.info("g2b %s~%s  %d/%d건", bgn.strftime("%m/%d"), end.strftime("%m/%d"),
                     min(page * self.num_rows, total), total)
            if page * self.num_rows >= total or not items:
                break
            page += 1
            time.sleep(0.3)

    # ── 변환 ──
    @staticmethod
    def _to_notice(it: dict) -> Notice:
        no = str(it.get("bidNtceNo", "")).strip()
        ord_ = str(it.get("bidNtceOrd", "00")).strip() or "00"
        url = it.get("bidNtceDtlUrl") or it.get("bidNtceUrl") or ""
        return Notice(
            source="g2b",
            notice_no=no,
            ord=ord_,
            title=str(it.get("bidNtceNm", "")).strip(),
            institution=str(it.get("ntceInsttNm", "")).strip(),
            demand_institution=str(it.get("dminsttNm", "")).strip(),
            notice_date=_fmt_dt(it.get("bidNtceDt") or it.get("rgstDt")),
            # 협상에 의한 계약 등은 bidClseDt(입찰마감)가 비어 오고 opengDt(개찰일시)만 있음 → 개찰일시를 마감으로 사용
            close_date=_fmt_dt(it.get("bidClseDt")) or _fmt_dt(it.get("opengDt")),
            open_date=_fmt_dt(it.get("opengDt")),
            estimated_price=_to_int(it.get("presmptPrce")),
            budget=_to_int(it.get("asignBdgtAmt")),
            contract_method=str(it.get("cntrctCnclsMthdNm", "")),
            award_method=str(it.get("sucsfbidMthdNm", "")),
            notice_kind=str(it.get("ntceKindNm", "")),
            service_div=str(it.get("srvceDivNm", "")),
            prespec_no=str(it.get("bfSpecRgstNo") or "").strip(),
            url=url,
            raw=json.dumps(it, ensure_ascii=False),
        )

    def collect(self, lookback_days: int) -> list[Notice]:
        end = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
        bgn = end - timedelta(days=lookback_days)
        insts = self.inst_filter or [None]
        out: dict[str, Notice] = {}
        # API 는 조회기간 제한이 있어 7일 단위로 잘라서 호출
        cur = bgn
        while cur < end:
            nxt = min(cur + timedelta(days=7), end)
            for inst in insts:
                for it in self._iter_items(cur, nxt, inst):
                    n = self._to_notice(it)
                    if n.notice_no:
                        out[n.key] = n
            cur = nxt
        return list(out.values())
