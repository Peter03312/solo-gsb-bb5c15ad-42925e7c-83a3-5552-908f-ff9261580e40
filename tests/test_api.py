"""端到端 API 测试：阶梯计提、分账舍入、退款冲销、关账批次、幂等与并发。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    body = {
        "work_id": work_id,
        "title": "测试作品",
        "tiers": tiers
        or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
        "authors": authors
        or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    }
    return client.post("/works", json=body)


def make_sale(client, sale_id, work_id="W1", copies=1, price="33.33", ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id,
            "work_id": work_id,
            "business_time": ts,
            "copies": copies,
            "net_price": price,
        },
    )


# ---------------------------------------------------------------- 作品校验


def test_work_ratio_sum_must_be_100(client):
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 60}])
    assert r.status_code == 422


def test_work_thresholds_must_increase(client):
    r = make_work(client, tiers=[{"threshold": 0, "rate": 0.08}, {"threshold": 0, "rate": 0.1}])
    assert r.status_code == 422


def test_work_first_threshold_must_be_zero(client):
    r = make_work(client, tiers=[{"threshold": 100, "rate": 0.08}])
    assert r.status_code == 422


def test_work_idempotent_replay_and_conflict(client):
    assert make_work(client).status_code == 201
    r = make_work(client)
    assert r.status_code == 200  # 同号同内容 → 返回原结果
    assert r.json()["work_id"] == "W1"
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 100}])
    assert r.status_code == 409  # 同号异内容 → 拒绝


# ---------------------------------------------------------------- 阶梯计提与分账


def test_sale_royalty_across_tiers(client):
    make_work(client)
    r = make_sale(client, "S1", copies=800)
    assert r.status_code == 201
    body = r.json()
    # 800 册全部落在第一档：33.33*0.08=2.6664 → 每册 267 分
    assert body["cum_before"] == 0 and body["cum_after"] == 800
    assert body["segments"] == [
        {
            "segment_index": 0,
            "tier_index": 0,
            "rate": "0.08",
            "copies": 800,
            "per_copy_royalty_cents": 267,
            "amount_cents": 213600,
            "refunded_copies": 0,
        }
    ]
    assert body["total_royalty_cents"] == 213600
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 128160},
        {"author_id": "A2", "amount_cents": 85440},
    ]

    # 跨档：累计销量 800→1200，前 200 册 8%，后 200 册 10%（每册 333 分）
    r = make_sale(client, "S2", copies=400)
    body = r.json()
    assert body["cum_before"] == 800 and body["cum_after"] == 1200
    assert [(s["tier_index"], s["copies"], s["per_copy_royalty_cents"]) for s in body["segments"]] == [
        (0, 200, 267),
        (1, 200, 333),
    ]
    assert body["total_royalty_cents"] == 200 * 267 + 200 * 333
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 72000},
        {"author_id": "A2", "amount_cents": 48000},
    ]


def test_split_remainder_filled_by_author_order(client):
    make_work(
        client,
        work_id="W2",
        tiers=[{"threshold": 0, "rate": 0.07}],
        authors=[
            {"author_id": "A3", "ratio": 33.34},
            {"author_id": "A1", "ratio": 33.33},
            {"author_id": "A2", "ratio": 33.33},
        ],
    )
    r = make_sale(client, "S9", work_id="W2", copies=1, price="10.00")
    body = r.json()
    assert body["total_royalty_cents"] == 70  # 10*0.07=0.70
    # 70*33.33%=23.31→23，70*33.34%=23.38→23，余 1 分按作者编号顺序补 A1
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 24},
        {"author_id": "A2", "amount_cents": 23},
        {"author_id": "A3", "amount_cents": 23},
    ]


def test_sale_idempotency(client):
    make_work(client)
    r1 = make_sale(client, "S1", copies=10)
    assert r1.status_code == 201
    r2 = make_sale(client, "S1", copies=10)
    assert r2.status_code == 200
    assert r1.json() == r2.json()  # 同号同内容 → 原结果
    r3 = make_sale(client, "S1", copies=11)
    assert r3.status_code == 409  # 同号异内容 → 拒绝


def test_sale_unknown_work_404(client):
    r = make_sale(client, "S1", work_id="NOPE")
    assert r.status_code == 404


# ---------------------------------------------------------------- 退款


def test_refund_reverses_from_last_copy(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)  # 200 册@267 + 200 册@333

    # 退 150 册：全部来自最后一档（每册 333 分）
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201
    body = r.json()
    assert body["total_refund_cents"] == 150 * 333
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(1, 150)]
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 29970},
        {"author_id": "A2", "amount_cents": 19980},
    ]

    # 再退 100 册：最后一档剩 50 册，继续冲倒数第二档 50 册
    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    body = r.json()
    assert body["total_refund_cents"] == 50 * 333 + 50 * 267
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(0, 50), (1, 50)]

    # 累计销量不回退
    sale = client.get("/sales/S2").json()
    assert sale["cum_after"] == 1200
    assert sale["refunded_copies"] == 250

    # 累计退款不得超过原册数：已退 250，再退 200 → 450 > 400
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 200})
    assert r.status_code == 400
    # 退满 400 册允许
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201


def test_refund_idempotency_and_unknown_sale(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    r1 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r1.status_code == 201
    r2 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    r3 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 4})
    assert r3.status_code == 409
    r4 = client.post("/refunds", json={"refund_id": "R9", "sale_id": "NOPE", "copies": 1})
    assert r4.status_code == 404


# ---------------------------------------------------------------- 关账


def test_close_batches_and_immutable_report(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})

    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 201
    report1 = r.json()
    assert report1["sales_count"] == 2 and report1["refunds_count"] == 1
    assert [l["type"] for l in report1["lines"]] == ["sale", "sale", "refund"]
    assert report1["total_sales_royalty_cents"] == 213600 + 120000
    assert report1["total_refund_cents"] == 49950
    assert report1["net_royalty_cents"] == 283650
    assert report1["author_totals"] == [
        {"author_id": "A1", "amount_cents": 128160 + 72000 - 29970},
        {"author_id": "A2", "amount_cents": 85440 + 48000 - 19980},
    ]

    # 同 request_id 重试 → 返回原报表
    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 200 and r.json()["close_id"] == report1["close_id"]

    # 关账后补录的旧业务流水进入下一批
    make_sale(client, "S3", copies=10, ts="2020-01-01T00:00:00")
    client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    r = client.post("/closes")
    report2 = r.json()
    assert report2["sales_count"] == 1 and report2["refunds_count"] == 1
    assert [(l["type"], l["sale_id"] if l["type"] == "sale" else l["refund_id"])
            for l in report2["lines"]] == [("sale", "S3"), ("refund", "R2")]
    assert report2["entry_seq_from"] == report1["entry_seq_to"] + 1

    # 第一批报表不可改：再次读取完全一致
    assert client.get(f"/closes/{report1['close_id']}").json() == report1
    # 流水归属批次已固定
    assert client.get("/sales/S1").json()["close_id"] == report1["close_id"]
    assert client.get("/sales/S3").json()["close_id"] == report2["close_id"]


def test_close_empty_batch(client):
    r = client.post("/closes")
    assert r.status_code == 201
    body = r.json()
    assert body["lines"] == [] and body["author_totals"] == []
    assert body["net_royalty_cents"] == 0


# ---------------------------------------------------------------- 结算付款


def settle(client, author_id="A1", close_id=1, payment_id="PAY-1", amount=1):
    return client.post(
        "/settlements",
        json={
            "author_id": author_id,
            "close_id": close_id,
            "payment_id": payment_id,
            "amount_cents": amount,
        },
    )


def test_settlement_flow_and_author_query(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 应计 128160，A2 应计 85440
    client.post("/closes")

    r = settle(client, amount=100000)
    assert r.status_code == 201
    body = r.json()
    assert body["payment_id"] == "PAY-1"
    assert body["author_id"] == "A1" and body["close_id"] == 1
    assert body["accrued_cents"] == 128160  # 截至该批次应计额
    assert body["paid_cents"] == 100000  # 含本笔的累计已付
    assert body["payable_cents"] == 28160  # 支付后剩余可付
    assert body["negative_balance_cents"] == 0

    # 按作者查询：分批应计/已付/负余额/可付/付款记录
    q = client.get("/authors/A1/settlement").json()
    assert q["latest_close_id"] == 1
    assert q["accrued_cents"] == 128160
    assert q["paid_cents"] == 100000
    assert q["payable_cents"] == 28160
    assert q["negative_balance_cents"] == 0
    assert q["batches"] == [{"close_id": 1, "amount_cents": 128160}]
    assert [(p["payment_id"], p["amount_cents"]) for p in q["payments"]] == [
        ("PAY-1", 100000)
    ]

    # 付清余款后可付额归零，再付 1 分也拒绝
    r = settle(client, payment_id="PAY-2", amount=28160)
    assert r.status_code == 201 and r.json()["payable_cents"] == 0
    assert settle(client, payment_id="PAY-3", amount=1).status_code == 400
    q = client.get("/authors/A1/settlement").json()
    assert q["paid_cents"] == 128160 and q["payable_cents"] == 0


def test_settlement_validation(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    client.post("/closes")
    # 作者不存在 / 批次不存在
    assert settle(client, author_id="NOPE", amount=1).status_code == 404
    assert settle(client, close_id=99, amount=1).status_code == 404
    # 支付金额须为正
    assert settle(client, amount=0).status_code == 422
    assert settle(client, amount=-5).status_code == 422
    # 超过可付额
    assert settle(client, amount=128161).status_code == 400
    # 失败的结算不占用付款编号：修正金额后可重新提交
    assert settle(client, amount=128160).status_code == 201
    # 批次并非当前最新：再关一批后，对旧批次付款拒绝
    make_sale(client, "S2", copies=1)
    client.post("/closes")
    assert settle(client, payment_id="PAY-9", close_id=1, amount=100).status_code == 409


def test_settlement_idempotent_replay_survives_later_closes(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    client.post("/closes")
    r1 = settle(client, amount=1000)
    assert r1.status_code == 201
    # 后续又入账并关账
    make_sale(client, "S2", copies=10)
    client.post("/closes")
    # 同付款编号同内容重试 → 返回原结算结果（即使批次已非最新）
    r2 = settle(client, amount=1000)
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 同号异内容 → 冲突
    assert settle(client, amount=1001).status_code == 409
    assert settle(client, close_id=2, amount=1000).status_code == 409
    # 按付款编号查询结果一致
    assert client.get("/settlements/PAY-1").json() == r1.json()
    assert client.get("/settlements/NOPE").status_code == 404
    assert client.get("/authors/NOPE/settlement").status_code == 404


def test_settlement_negative_balance_offset(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 应计 128160
    client.post("/closes")
    # 全额付款
    r = settle(client, amount=128160)
    assert r.status_code == 201 and r.json()["payable_cents"] == 0

    # 关账后退款 100 册：第二批 A1 净额 -16020，应计低于已付 → 待抵扣负余额
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    client.post("/closes")
    q = client.get("/authors/A1/settlement").json()
    assert q["batches"] == [
        {"close_id": 1, "amount_cents": 128160},
        {"close_id": 2, "amount_cents": -16020},
    ]
    assert q["accrued_cents"] == 112140
    assert q["paid_cents"] == 128160
    assert q["payable_cents"] == 0
    assert q["negative_balance_cents"] == 16020
    # 可付额为 0，任何正额支付都拒绝
    assert settle(client, payment_id="PAY-2", close_id=2, amount=1).status_code == 400

    # 后续关账正额（A1 +32040）先抵扣负余额，剩余 16020 进入可付
    make_sale(client, "S2", copies=200)
    client.post("/closes")
    q = client.get("/authors/A1/settlement").json()
    assert q["accrued_cents"] == 144180
    assert q["payable_cents"] == 16020
    assert q["negative_balance_cents"] == 0
    r = settle(client, payment_id="PAY-2", close_id=3, amount=16020)
    assert r.status_code == 201 and r.json()["payable_cents"] == 0
    # 旧报表与旧付款不改写
    assert client.get("/closes/1").json()["author_totals"] == [
        {"author_id": "A1", "amount_cents": 128160},
        {"author_id": "A2", "amount_cents": 85440},
    ]
    assert client.get("/settlements/PAY-1").json()["amount_cents"] == 128160


# ---------------------------------------------------------------- 并发


def test_concurrent_sales_and_close(client):
    make_work(client)
    n = 40

    def post_sale(i):
        return client.post(
            "/sales",
            json={
                "sale_id": f"CS{i}",
                "work_id": "W1",
                "business_time": "2026-09-01T10:00:00",
                "copies": 1,
                "net_price": "10.00",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(post_sale, i) for i in range(n)]
        mid_close = client.post("/closes")  # 与入账并发，归属由提交顺序决定
        results = [f.result() for f in futures]
    assert all(r.status_code == 201 for r in results)
    final_close = client.post("/closes")

    # 累计销量无丢无重：cum_after 恰好是 1..n 的排列
    cums = sorted(client.get(f"/sales/CS{i}").json()["cum_after"] for i in range(n))
    assert cums == list(range(1, n + 1))

    # 每笔流水恰好归属一个批次，且两批区间首尾相接
    c1, c2 = mid_close.json(), final_close.json()
    assert c1["sales_count"] + c2["sales_count"] == n
    assert c2["entry_seq_from"] == c1["entry_seq_to"] + 1
    ids = [l["sale_id"] for l in c1["lines"]] + [l["sale_id"] for l in c2["lines"]]
    assert sorted(ids) == sorted(f"CS{i}" for i in range(n))


def test_concurrent_settlements_never_overpay(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 可付 128160
    client.post("/closes")

    def pay(i):
        return settle(client, payment_id=f"CP{i}", amount=20000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pay, range(10)))
    ok = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 400]
    # 6×20000=120000 ≤ 128160 < 7×20000：恰好 6 笔成功，其余因超额拒绝
    assert len(ok) == 6 and len(rejected) == 4
    q = client.get("/authors/A1/settlement").json()
    assert q["paid_cents"] == 120000  # 已付不超过应计，并发未超付
    assert q["payable_cents"] == 8160
