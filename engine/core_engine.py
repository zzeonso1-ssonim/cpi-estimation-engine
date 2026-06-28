"""
근원 계산 — 재정규화(조정가중치) + bottom-up MoM 합성.
근거: PRD v3.1 §1.3, §5.3.

핵심: 제외품목을 뺀 뒤 포함품목 가중치 합이 다시 1000이 되도록 재정규화한다.
누락하면 근원 MoM이 체계적으로 과소추정됨(§1.3).
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Bucket:
    code: str
    name: str
    weight: float        # 원가중치(천분비)
    in_core1: int
    in_core2: int


@dataclass
class RenormResult:
    core: str                       # 'core1' | 'core2'
    adj_weights: dict               # code -> 조정가중치
    sum_adj: float                  # Σ조정가중치 (검증6: =1000 ±0.1)
    included_raw_sum: float         # 포함 버킷 원가중치 단순합
    excluded_raw_sum: float
    published_sum: float | None     # 공표 근원합(앵커)
    residual_vs_published: float | None  # 단순합 − 공표합 (혼재버킷 잔차, §1.3)
    tier: int = 1                   # 1=버킷 all-or-nothing, 2=품목 부분포함
    incl_bucket_weight: dict = field(default_factory=dict)  # tier2: 버킷별 근원포함 품목가중치합


def renormalize(buckets: list[Bucket], core: str,
                published_sum: float | None = None) -> RenormResult:
    """
    조정가중치_i = 원가중치_i × 1000 / Σ(포함품목 원가중치)   (§5.3)

    Σ조정가중치는 구성상 항상 1000 → 검증6 통과.
    published_sum이 주어지면 단순합과의 잔차를 진단으로 노출(Tier 1 혼재버킷 한계).
    """
    flag = "in_core1" if core == "core1" else "in_core2"
    included = [b for b in buckets if getattr(b, flag) == 1]
    excluded = [b for b in buckets if getattr(b, flag) == 0]

    included_raw_sum = sum(b.weight for b in included)
    excluded_raw_sum = sum(b.weight for b in excluded)
    if included_raw_sum <= 0:
        raise ValueError("포함 품목 가중치 합이 0 — 근원 정의 확인 필요")

    adj = {b.code: b.weight * 1000 / included_raw_sum for b in included}
    sum_adj = sum(adj.values())

    residual = (included_raw_sum - published_sum) if published_sum is not None else None
    return RenormResult(
        core=core, adj_weights=adj, sum_adj=sum_adj,
        included_raw_sum=included_raw_sum, excluded_raw_sum=excluded_raw_sum,
        published_sum=published_sum, residual_vs_published=residual,
    )


def included_bucket_weights(items: dict, core: str) -> dict:
    """버킷별, 해당 근원에 포함되는 *품목* 가중치 합 (Tier 2).
    items: name -> {'weight','bucket','c1','c2'} (db.load_items()).
    예: 농산물 버킷 중 곡물(쌀 등)만 core②에 포함 → 농산물의 부분 가중치만 잡힘."""
    flag = "c1" if core == "core1" else "c2"
    out: dict = {}
    for v in items.values():
        if v[flag] == 1:
            out[v["bucket"]] = out.get(v["bucket"], 0.0) + v["weight"]
    return out


def renormalize_tier2(items: dict, core: str,
                      published_sum: float | None = None) -> RenormResult:
    """
    품목 단위 core 플래그로 버킷별 '실제 포함 가중치'를 구해 재정규화(§5.3 + §1.3 정밀화).

    버킷 all-or-nothing(Tier1) 대신 버킷의 근원 포함 *부분*만 잡으므로
    Σ(포함 품목가중치)가 공표 근원합에 훨씬 근접 → 혼재버킷 잔차 축소.
    조정가중치_b = 포함품목가중치_b × 1000 / Σ(포함품목가중치)  (버킷 b 단위, MoM은 버킷 가정 사용)
    """
    incl = included_bucket_weights(items, core)
    included_raw_sum = sum(incl.values())
    if included_raw_sum <= 0:
        raise ValueError("포함 품목 가중치 합이 0 — 품목 core 플래그 확인 필요")
    adj = {code: w * 1000 / included_raw_sum for code, w in incl.items()}
    residual = (included_raw_sum - published_sum) if published_sum is not None else None
    return RenormResult(
        core=core, adj_weights=adj, sum_adj=sum(adj.values()),
        included_raw_sum=included_raw_sum, excluded_raw_sum=0.0,
        published_sum=published_sum, residual_vs_published=residual,
        tier=2, incl_bucket_weight=incl,
    )


def core_mom(renorm: RenormResult, bucket_mom: dict) -> float:
    """
    Core_MoM = Σ(포함품목 조정가중치_i / 1000 × 품목_MoM_i)   (§5.3)
    bucket_mom: code -> MoM(%). 포함 버킷만 사용.
    Tier2도 동일 — adj_weights가 버킷 단위라 버킷 MoM을 그대로 곱한다.
    """
    return sum(w / 1000 * bucket_mom.get(code, 0.0)
               for code, w in renorm.adj_weights.items())
