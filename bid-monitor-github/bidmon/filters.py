from .models import Notice


# 기관명이 바뀐 경우를 위한 보조 별칭 (config.yaml 에 없어도 태그가 붙도록)
_ALIASES = {
    "SH": ["서울주택도시개발공사", "서울주택도시공사", "SH공사"],
    "GH": ["경기주택도시공사", "경기도시공사"],
    "IH": ["인천도시공사"],
    "LH": ["한국토지주택공사"],
}


def tag_institutions(n: Notice, institutions: dict[str, list[str]]) -> None:
    hay = f"{n.institution} {n.demand_institution}"
    tags = []
    for tag, names in institutions.items():
        if any(x in hay for x in list(names) + _ALIASES.get(tag, [])):
            tags.append(tag)
    n.tags = ",".join(tags)


def apply_keyword_filter(n: Notice, include: list[str], exclude: list[str]) -> None:
    title = n.title.replace(" ", "")
    inc = [k for k in include if k.replace(" ", "") in title]
    exc = [k for k in exclude if k.replace(" ", "") in title]
    # 포함 키워드가 있고, 제외 키워드가 없으면 관심 공고
    # 단, "설계공모/설계용역" 처럼 강한 키워드는 제외어("공사")가 있어도 살림 (예: "OO공사 설계용역")
    n.matched = int(bool(inc) and not exc)
    n.matched_keywords = ",".join(dict.fromkeys(k.replace(" ", "") for k in inc))


import re as _re

_PUBLIC = ("공사", "공단", "진흥원", "재단", "연구원", "연구소", "사업단", "공기업", "공공기관", "센터", "협회", "공제", "공제회", "기금", "관리원", "평가원", "심사")
_EDU = ("교육청", "교육지원청", "학교", "대학", "교육원")
_LOCAL_HINT = ("특별시", "광역시", "특별자치", "시청", "군청", "구청", "도청")
_LOCAL_RE = _re.compile(r"(?:^|\s)\S+(?:시|군|구|도)(?:$|\s)")
_CENTRAL_RE = _re.compile(r"(?:부|처|청|위원회|원|본부|사령부|국)$")


def categorize(n: Notice) -> None:
    """공고기관명으로 카테고리 부여: 조합 / 공공기관 / 교육기관 / 지자체 / 중앙정부 / 기타"""
    name = (n.institution or "").strip()
    if not name or "조달청" in name or "지방조달청" in name:   # 조달청 대행이면 실제 수요기관 기준
        name = (n.demand_institution or name).strip()
    if not name:
        n.category = "기타"; return
    if n.source in ("seoul_cleanup", "busan_dynamice") or any(k in name for k in ("정비사업조합", "재개발", "재건축", "추진위원회", "정비사업")):
        n.category = "조합"
    elif any(k in name for k in _EDU):
        n.category = "교육기관"
    elif any(k in name for k in _PUBLIC):
        n.category = "공공기관"
    elif any(k in name for k in _LOCAL_HINT) or _LOCAL_RE.search(name):
        n.category = "지자체"
    elif _CENTRAL_RE.search(name):
        n.category = "중앙정부"
    else:
        n.category = "기타"
