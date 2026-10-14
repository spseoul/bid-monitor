"""
나라장터 API → docs/index.html 생성 (GitHub Actions 가 매일 실행)
- 이전 index.html 에 담긴 공고를 읽어와서, 최근 며칠치만 새로 받아 합친다 (증분 수집)
- 수집이 0건이면 빈 페이지를 올리지 않고 실패 처리 (이전 페이지 유지)
"""
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)
from pathlib import Path

from bidmon.config import load_config, ROOT
from bidmon.filters import tag_institutions, categorize
from bidmon.collectors import G2BCollector, G2BPrespecCollector, LHCollector, LHBoardCollector, SeoulCleanupCollector, BusanDynamiceCollector, SHBoardCollector, GHBoardCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build")

DOCS = ROOT / "docs"
INDEX = DOCS / "index.html"
SEEN = DOCS / "seen.json"
KEEP = ["key", "notice_no", "title", "institution", "demand_institution", "notice_date", "close_date",
        "estimated_price", "budget", "award_method", "notice_kind", "url", "tags", "category"]


def load_previous() -> dict[str, dict]:
    """이전 index.html 안의 DATA 를 key→row 로 복원"""
    if not INDEX.exists():
        return {}
    m = re.search(r"const DATA=(\[.*?\]);\n", INDEX.read_text(encoding="utf-8"), re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return {r["key"]: r for r in rows if r.get("key")}


def main():
    cfg = load_config()
    keep_days = int(cfg.get("lookback_days", 30))
    prev = load_previous()
    days = int(cfg.get("incremental_days", 3)) if prev else keep_days
    log.info("이전 공고 %d건, 이번 수집 범위 최근 %d일", len(prev), days)

    collectors = [G2BCollector(cfg["g2b"])]
    if (cfg.get("g2b_prespec") or {}).get("enabled"):
        collectors.append(G2BPrespecCollector({**cfg["g2b"], **cfg["g2b_prespec"]}))
    lh_cfg = cfg.get("lh") or {}
    if lh_cfg.get("enabled", True):          # config.yaml 에 lh 항목이 없어도 기본 켜짐
        lh = LHCollector(lh_cfg)
        lh.known = set(prev)                 # 이미 페이지에 있는 공고는 상세 조회 생략
        collectors.append(lh)
    lb_cfg = cfg.get("lh_board") or {}
    if lb_cfg.get("enabled", True):          # LH 홈페이지 공모·공고 게시판 (설계공모 등)
        lb = LHBoardCollector(lb_cfg)
        lb.known = set(prev)
        collectors.append(lb)
    sc_cfg = cfg.get("seoul_cleanup") or {}
    if sc_cfg.get("enabled", True):          # 서울 정비사업 정보몽땅 조합입찰공고
        sc = SeoulCleanupCollector(sc_cfg)
        sc.known = set(prev)
        collectors.append(sc)
    bd_cfg = cfg.get("busan_dynamice") or {}
    if bd_cfg.get("enabled", True):          # 부산 정비사업 통합홈페이지 조합입찰공고
        bd = BusanDynamiceCollector(bd_cfg)
        bd.known = set(prev)
        collectors.append(bd)
    for C, key in ((SHBoardCollector, "sh_board"), (GHBoardCollector, "gh_board")):   # SH 현상설계 / GH 공모 게시판
        c_cfg = cfg.get(key) or {}
        if c_cfg.get("enabled", True):
            c = C(c_cfg); c.known = set(prev); collectors.append(c)
    notices = []
    for c in collectors:
        notices += c.safe_collect(days)
    if not notices:
        log.error("수집 결과 0건 — API 오류 또는 호출 한도 초과 가능. 이전 페이지를 유지합니다.")
        sys.exit(1)

    seen = json.loads(SEEN.read_text(encoding="utf-8")) if SEEN.exists() else {}
    today = now_kst().strftime("%Y-%m-%d")
    for n in notices:
        tag_institutions(n, cfg["institutions"])
        categorize(n)
        d = {k: getattr(n, k) for k in KEEP}
        old = prev.get(n.key) or {}
        for k in ("notice_date", "estimated_price", "budget", "demand_institution"):
            if not d.get(k) and old.get(k):  # 이번에 비어 있으면 이전 값 유지 (LH 상세 생략 시)
                d[k] = old[k]
        d["first_seen"] = seen.setdefault(n.key, today)
        prev[n.key] = d          # 새 정보로 덮어씀 (변경공고 반영)

    cutoff = (now_kst() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    today_s = now_kst().strftime("%Y-%m-%d")
    # 최근 N일 공고 + (공고일이 오래됐어도) 아직 마감 전인 공고는 유지 (공고~개찰 한 달 넘는 용역 누락 방지)
    data = [r for r in prev.values()
            if (r.get("notice_date") or "") >= cutoff or (r.get("close_date") or "")[:10] >= today_s]
    for r in data:
        r.setdefault("first_seen", seen.get(r["key"], today))
    data.sort(key=lambda r: r["notice_date"] or "", reverse=True)
    seen = {k: v for k, v in seen.items() if v >= (now_kst() - timedelta(days=60)).strftime("%Y-%m-%d")}

    kw = {"include": cfg["filters"]["include_keywords"], "exclude": cfg["filters"]["exclude_keywords"]}
    # config.yaml 에서 filters 앞부분(주석·기타 설정)을 그대로 보존 → 관리자 모드에서 파일 재생성 시 사용
    raw = (ROOT / "config.yaml").read_text(encoding="utf-8")
    head = raw.split("filters:")[0] if "filters:" in raw else ""
    admin_hash = os.getenv("ADMIN_PW_HASH", "").strip() or hashlib.sha256(
        (cfg.get("admin_password") or "sps-admin").encode()).hexdigest()
    now = now_kst()
    html = ((ROOT / "bidmon" / "page.html").read_text(encoding="utf-8")
            .replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("__KW__", json.dumps(kw, ensure_ascii=False))
            .replace("__CFGHEAD__", json.dumps(head, ensure_ascii=False))
            .replace("__ADMIN_HASH__", admin_hash)
            .replace("__GH_REPO__", cfg.get("github_repo", ""))
            .replace("__GH_PATH__", cfg.get("github_config_path", ""))
            .replace("__TOKEN_ENC__", str(cfg.get("admin_token_enc", "") or ""))
            .replace("__DATE__", now.strftime("%Y-%m-%d %H:%M"))
            .replace("__NOW__", now.strftime("%Y-%m-%dT%H:%M:%S")))
    DOCS.mkdir(exist_ok=True)
    INDEX.write_text(html, encoding="utf-8")
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("index.html 생성: 신규 수집 %d건, 페이지 총 %d건, %.1f MB", len(notices), len(data), INDEX.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
