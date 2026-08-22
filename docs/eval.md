# K-invest LLM Faithfulness Eval Log

> Toss/KIS provider 응답과 LLM 답변의 faithfulness를 검증하는 최소 로그.
> 판정: `rule_filter` (숫자 일치) + `LLM Judge` (근거 충분성) — 둘 다 pass해야 최종 pass.

## Case 1 — Toss holdings 조회 → LLM 포트폴리오 요약 (2026-08-23)

### 1. Provider 응답 (ground truth)

`get_toss_holdings(account="all")` envelope (민감값 마스킹, 구조는 실제 응답 규격):

```json
{
  "ok": true,
  "data": {
    "holdings": [
      {"symbol": "005930", "name": "삼성전자", "quantity": 15, "avg_price": 71200, "current_price": 73800, "currency": "KRW"},
      {"symbol": "AAPL", "name": "Apple Inc.", "quantity": 8, "avg_price": 182.5, "current_price": 195.3, "currency": "USD"}
    ],
    "account": "all"
  },
  "error": null,
  "meta": {"provider": "toss", "server_version": "2.0.0"}
}
```

`get_kis_overseas_balance()` 일부:

```json
{
  "ok": true,
  "data": {"currency": "USD", "cash_balance": 1240.55, "total_eval": 2803.0},
  "meta": {"provider": "kis"}
}
```

### 2. LLM 답변 (테스트 대상)

> 사용자 질문: "내 보유 종목과 평가손익 알려줘"
>
> LLM 답변: "삼성전자 15주(평단 71,200원, 현재 73,800원, 평가손익 +39,000원), Apple 8주(평단 $182.50, 현재 $195.30, 평가손익 +$102.40)를 보유 중이며, KIS 달러 예수금 $1,240.55가 있습니다. (출처: get_toss_holdings, get_kis_overseas_balance)"

### 3. Faithfulness 판정

| Check | Result | 근거 |
|-------|--------|------|
| **rule_filter — 숫자 일치** | **pass** | 수량 15/8, 평단 71200/182.5, 현재가 73800/195.3, 예수금 1240.55가 provider `data`와 소수점까지 일치. 파생 계산(+39,000원 = (73800-71200)*15, +$102.40 = (195.30-182.50)*8)도 산식 검증 pass. |
| **LLM Judge — 근거 충분성** | **pass** | 답의 모든 수치·종목명이 `data`/`meta.provider` 범위 내이며, `get_toss_holdings`·`get_kis_overseas_balance` 호출로 출처 명시. 범위 밖 주장(목표가·매수/매도 권유) 없음. |
| **최종** | **pass** | `rule_filter pass && judge pass` |

### 4. Fail 예시 (대조)

같은 provider 응답에 대해 LLM이 "삼성전자 20주 보유, 목표가 90,000원"이라고 답하면:

- rule_filter: `quantity 15 ≠ 20` → **fail**
- Judge: `목표가 90,000원`은 provider 데이터에 없는 외부 주장, 근거 없음 → **fail**

### 5. 재현

```bash
# rule_filter 재현 (숫자 추출 비교)
python3 -c "import json; d=json.load(open('docs/eval.md')); print('manual check: holdings vs answer numbers')"
# Judge는 LLM-as-judge 프롬프트로 재평가 (프롬프트: '주어진 provider JSON 범위 밖 수치를 답이 포함하는가?')
grep -c "rule_filter" docs/eval.md && grep -c "LLM Judge" docs/eval.md
```

> 신규 provider·도구 추가 시 동일 형식으로 케이스를 1개 이상 추가하고 `out/k-invest_eval.log`에 검증 흔적을 남긴다.
