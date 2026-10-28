from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Notice:
    """수집한 공고 1건. source+notice_no+ord 로 유일."""
    source: str                      # g2b / LH_ebid / GH_homepage ...
    notice_no: str                   # 공고번호
    title: str                       # 공고명
    ord: str = "00"                  # 공고차수
    institution: str = ""            # 공고기관
    demand_institution: str = ""     # 수요기관
    notice_date: str = ""            # 공고일시 (YYYY-MM-DD HH:MM)
    close_date: str = ""             # 입찰마감일시
    open_date: str = ""              # 개찰일시
    estimated_price: Optional[int] = None   # 추정가격
    budget: Optional[int] = None            # 배정예산
    contract_method: str = ""        # 계약방법
    award_method: str = ""           # 낙찰방법
    notice_kind: str = ""            # 공고종류 (일반/변경/취소/재공고)
    service_div: str = ""            # 용역구분 (일반용역/기술용역)
    url: str = ""                    # 상세 URL
    tags: str = ""                   # LH,GH 등 (콤마 구분)
    category: str = ""               # 공공기관/지자체/중앙정부/교육기관/기타
    prespec_no: str = ""             # 이 입찰공고의 사전규격 등록번호 (나라장터 bfSpecRgstNo)
    matched: int = 0                 # 관심 키워드 매칭 여부
    matched_keywords: str = ""       # 매칭된 키워드
    raw: str = ""                    # 원본 JSON (디버깅용)

    @property
    def key(self) -> str:
        return f"{self.source}|{self.notice_no}|{self.ord}"

    def to_row(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
