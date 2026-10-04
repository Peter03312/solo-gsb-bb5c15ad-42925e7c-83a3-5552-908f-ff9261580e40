#!/usr/bin/env bash
# 端到端调用示例：录入作品 → 销售 → 退款 → 关账 → 查看报表 → 结算付款 → 作者结算查询
# 用法：BASE_URL=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
fmt() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1. 录入作品（两档阶梯 + 两位作者 60/40 分成）=="
curl -s -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [
    {"threshold": 0,    "rate": 0.08},
    {"threshold": 1000, "rate": 0.10}
  ],
  "authors": [
    {"author_id": "A1", "ratio": 60},
    {"author_id": "A2", "ratio": 40}
  ]
}' | fmt

echo "== 2. 登记销售 1200 册（跨档：1000 册 8% + 200 册 10%）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S100",
  "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200,
  "net_price": 33.33
}' | fmt

echo "== 3. 退款 250 册（从最后一册起冲回：200 册 10% 档 + 50 册 8% 档）=="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100",
  "sale_id": "S100",
  "copies": 250
}' | fmt

echo "== 4. 关账，生成不可改报表 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}' | fmt

echo "== 5. 关账后补录旧业务流水（计入下一批）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S101",
  "work_id": "BOOK-1",
  "business_time": "2026-08-15T08:00:00",
  "copies": 5,
  "net_price": 20.00
}' | fmt

echo "== 6. 再次关账并查看两批报表 =="
curl -s -X POST "$BASE/closes" | fmt
curl -s "$BASE/closes" | fmt

echo "== 7. 结算付款：对当前最新批次确认作者 A1 实际付款 =="
LATEST_CLOSE=$(curl -s "$BASE/closes" | python3 -c "import sys, json; print(json.load(sys.stdin)['closes'][-1]['close_id'])")
curl -s -X POST "$BASE/settlements" -H 'Content-Type: application/json' -d "{
  \"author_id\": \"A1\",
  \"close_id\": ${LATEST_CLOSE},
  \"payment_id\": \"PAY-2026-09-A1\",
  \"amount_cents\": 100000
}" | fmt

echo "== 8. 同号同内容重试（返回原结算结果）与按作者查询结算 =="
curl -s -X POST "$BASE/settlements" -H 'Content-Type: application/json' -d "{
  \"author_id\": \"A1\",
  \"close_id\": ${LATEST_CLOSE},
  \"payment_id\": \"PAY-2026-09-A1\",
  \"amount_cents\": 100000
}" | fmt
curl -s "$BASE/authors/A1/settlement" | fmt
