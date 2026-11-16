"""Deterministic synthetic promotion data.

The design intentionally contains the two things that make the naive comparison
wrong, so the app has something real to show:

* non-random store grouping -- promotion stores are larger, busier and more
  urban, and those same covariates lift spend per member;
* late refunds -- a meaningful share of refunds land after the analysis cutoff
  and the promotion group has a higher refund rate, so an as-of-cutoff report
  overstates net revenue for that group.

Files written:
    stores.csv
    orders.csv
    exposure.csv
    refunds_initial.csv     refunds known at the cutoff
    refunds_backfill.csv    refunds that arrived later
    truth.json              design parameters, for tests only (not shown in UI)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .db import init_db
from .ingest import create_snapshot, ingest_csv
from .versions import seed_versions

SEED = 20260615
CAMPAIGN_START = date(2026, 6, 29)
CAMPAIGN_END = date(2026, 8, 23)
PRE_START = date(2026, 4, 6)
PRE_END = date(2026, 6, 28)
REFUND_CUTOFF = "2026-09-06T23:59:59Z"

REGIONS = ["华东", "华南", "华北", "西南", "华中", "东北"]
FORMATS = ["大卖场", "标准超市", "社区店", "便利店"]
REGION_EFFECT = {"华东": 0.08, "华南": 0.04, "华北": 0.01, "西南": -0.02, "华中": 0.0, "东北": -0.06}
FORMAT_BASKET = {"大卖场": 330.0, "标准超市": 250.0, "社区店": 180.0, "便利店": 110.0}
FORMAT_RATE = {"大卖场": 1.10, "标准超市": 1.00, "社区店": 0.85, "便利店": 0.70}
FORMAT_SIZE = {"大卖场": 8000.0, "标准超市": 3200.0, "社区店": 1100.0, "便利店": 350.0}
FORMAT_MEMBERS = {"大卖场": 5200, "标准超市": 3200, "社区店": 1600, "便利店": 750}
CHANNELS = ["app", "store", "wechat", "sms", "email"]
CHANNEL_WEIGHTS = [0.42, 0.28, 0.16, 0.09, 0.05]
STATUSES = ["completed", "shipped", "pending", "cancelled", "returned"]
STATUS_WEIGHTS = [0.88, 0.06, 0.02, 0.02, 0.02]
TRUE_ORDER_LIFT = 0.07
PROMO_REFUND_RATE = 0.060
CONTROL_REFUND_RATE = 0.038
BASE_REFUND_RATE = 0.040


def _ts(day: date, seconds: int) -> str:
    return datetime.combine(day, time(0, 0), tzinfo=timezone.utc).replace(second=0) + timedelta(
        seconds=seconds
    ) if False else (
        datetime.combine(day, time(0, 0), tzinfo=timezone.utc) + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _logistic(rng: random.Random) -> float:
    # logistic(0, 1)
    u = rng.random()
    return math.log(u / (1 - u))


def _weighted(rng: random.Random, values: list[Any], weights: list[float]) -> Any:
    r = rng.random() * sum(weights)
    upto = 0.0
    for value, weight in zip(values, weights):
        upto += weight
        if r <= upto:
            return value
    return values[-1]


def _days(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def build_stores(rng: random.Random) -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []
    index = 0
    for region in REGIONS:
        for fmt in FORMATS:
            for local in range(3):
                index += 1
                size = FORMAT_SIZE[fmt] * rng.uniform(0.75, 1.3)
                traffic = min(2.4, max(0.35, 1.05 + 0.35 * _logistic(rng) + (0.18 if fmt == "大卖场" else 0)))
                urban = min(0.97, max(0.12, 0.55 + 0.17 * _logistic(rng) + (0.08 if region == "华东" else 0.0)))
                member_share = min(0.92, max(0.25, 0.60 + 0.10 * _logistic(rng)))
                members = int(FORMAT_MEMBERS[fmt] * rng.uniform(0.75, 1.25))
                stores.append(
                    {
                        "store_id": f"S{index:03d}",
                        "store_name": f"{region}{fmt}{local + 1}店",
                        "region": region,
                        "format": fmt,
                        "cluster": f"{region}-{fmt}",
                        "size_sqm": round(size, 1),
                        "traffic_index": round(traffic, 3),
                        "urban_score": round(urban, 3),
                        "customer_count": members,
                        "member_share": round(member_share, 3),
                    }
                )
    _assign_groups(rng, stores)
    return stores


def _assign_groups(rng: random.Random, stores: list[dict[str, Any]]) -> None:
    """Promotion assignment is NOT random: it depends on the same measured
    covariates that drive spend.  Three region x format cells deliberately have
    no control store, so stratification has genuinely excluded stores to show."""
    unsupported = {("东北", "便利店"), ("华中", "大卖场"), ("西南", "社区店")}
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for store in stores:
        cells.setdefault((store["region"], store["format"]), []).append(store)
    for key, group in sorted(cells.items()):
        if key in unsupported:
            for store in group:
                store["store_group"] = "promo"
            continue
        for store in group:
            logit = (
                0.45
                + 0.60 * (store["urban_score"] - 0.55)
                + 0.35 * (store["traffic_index"] - 1.1)
                + 0.30 * (math.log(store["size_sqm"]) - math.log(1400))
                + 0.25 * (store["member_share"] - 0.60)
                + (0.10 if store["region"] == "华东" else 0.0)
            )
            store["store_group"] = "promo" if rng.random() < _sigmoid(logit) else "control"


def _seasonal(day: date) -> float:
    idx = (day - PRE_START).days
    wave = 1.0 + 0.05 * math.sin(2 * math.pi * idx / 150.0)
    weekend = 1.08 if day.weekday() >= 5 else 1.0
    return wave * weekend


def _basket_mean(store: dict[str, Any]) -> float:
    base = FORMAT_BASKET[store["format"]]
    return base * (0.78 + 0.55 * store["urban_score"]) * (0.90 + 0.20 * store["member_share"]) * (
        1.0 + REGION_EFFECT[store["region"]] * 0.6
    )


def generate(
    output_dir: Path,
    seed: int = SEED,
    order_scale: float = 1.0,
) -> dict[str, Any]:
    rng = random.Random(seed)
    stores = build_stores(rng)
    output_dir.mkdir(parents=True, exist_ok=True)

    with (output_dir / "stores.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "store_id", "store_name", "region", "format", "cluster", "size_sqm",
                "traffic_index", "urban_score", "customer_count", "member_share", "store_group",
            ]
        )
        for store in stores:
            writer.writerow(
                [
                    store["store_id"], store["store_name"], store["region"], store["format"],
                    store["cluster"], store["size_sqm"], store["traffic_index"],
                    store["urban_score"], store["customer_count"], store["member_share"],
                    store["store_group"],
                ]
            )

    order_file = (output_dir / "orders.csv").open("w", newline="", encoding="utf-8")
    exposure_file = (output_dir / "exposure.csv").open("w", newline="", encoding="utf-8")
    refund_initial_file = (output_dir / "refunds_initial.csv").open("w", newline="", encoding="utf-8")
    refund_backfill_file = (output_dir / "refunds_backfill.csv").open("w", newline="", encoding="utf-8")
    order_writer = csv.writer(order_file)
    exposure_writer = csv.writer(exposure_file)
    refund_initial_writer = csv.writer(refund_initial_file)
    refund_backfill_writer = csv.writer(refund_backfill_file)
    order_writer.writerow(
        [
            "order_id", "customer_id", "store_id", "channel", "ordered_at", "status",
            "gross_cents", "discount_cents", "net_cents",
        ]
    )
    exposure_writer.writerow(
        ["exposure_id", "customer_id", "store_id", "channel", "exposure_kind", "exposed_at", "units", "amount_cents"]
    )
    refund_header = ["refund_id", "order_id", "customer_id", "store_id", "refunded_at", "amount_cents"]
    refund_initial_writer.writerow(refund_header)
    refund_backfill_writer.writerow(refund_header)

    cutoff = datetime.fromisoformat(REFUND_CUTOFF.replace("Z", "+00:00"))
    order_seq = 0
    exposure_seq = 0
    refund_init_seq = 0
    refund_back_seq = 0
    refunded_orders = 0
    late_refund_orders = 0
    campaign_orders = 0
    pre_orders = 0
    exposure_rows = 0

    for store in stores:
        store_noise = math.exp(rng.gauss(0, 0.055))
        basket_noise = math.exp(rng.gauss(0, 0.035))
        trend_gain = max(-0.08, 0.16 * (store["urban_score"] - 0.30) + rng.gauss(0, 0.05))
        member_ids = [f"C{store['store_id']}-{i:05d}" for i in range(1, store["customer_count"] + 1)]
        for day in _days(PRE_START, CAMPAIGN_END):
            in_campaign = CAMPAIGN_START <= day <= CAMPAIGN_END
            promo_day = in_campaign and store["store_group"] == "promo"
            multiplier = (1.0 + TRUE_ORDER_LIFT) if promo_day else 1.0
            # Secular growth is correlated with the same covariates that drive
            # promotion assignment, so a simple before/after difference still
            # carries selection bias.  This is what matching/stratification is
            # meant to expose and partly remove.
            progress = (day - PRE_START).days / float((CAMPAIGN_END - PRE_START).days)
            trend = 1.0 + trend_gain * progress
            rate = 0.0042 * FORMAT_RATE[store["format"]] * _seasonal(day) * store_noise * multiplier * trend
            mean_orders = max(0.0, store["customer_count"] * rate * order_scale)
            count = int(round(rng.gauss(mean_orders, math.sqrt(mean_orders) * 1.05)))
            count = max(0, count)
            day_base = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
            for _ in range(count):
                customer_id = rng.choice(member_ids)
                seconds = rng.randrange(8 * 3600, 22 * 3600)
                ordered_dt = day_base + timedelta(seconds=seconds)
                status = _weighted(rng, STATUSES, STATUS_WEIGHTS)
                basket = _basket_mean(store) * basket_noise * math.exp(rng.gauss(0, 0.42))
                gross = max(50, int(round(basket * 100)))
                discount_rate = min(0.35, max(0.0, rng.gauss(0.08, 0.045)))
                discount = int(round(gross * discount_rate))
                net = gross - discount
                order_seq += 1
                order_id = f"O{order_seq:08d}"
                channel = _weighted(rng, CHANNELS, CHANNEL_WEIGHTS)
                order_writer.writerow(
                    [order_id, customer_id, store["store_id"], channel, _iso(ordered_dt), status, gross, discount, net]
                )
                if PRE_START <= day <= PRE_END:
                    pre_orders += 1
                else:
                    campaign_orders += 1

                # Conversion-linked exposure: a slice of campaign orders is
                # preceded by a campaign exposure.  Regular exposures occur
                # for every store, so attribution is not perfectly collinear
                # with the store group.
                if promo_day and status in ("completed", "shipped") and rng.random() < 0.24:
                    hours = rng.uniform(2.0, 36.0)
                    exposed_dt = max(
                        datetime.combine(CAMPAIGN_START, time(0, 0), tzinfo=timezone.utc),
                        ordered_dt - timedelta(hours=hours),
                    )
                    exposure_seq += 1
                    exposure_writer.writerow(
                        [
                            f"E{exposure_seq:08d}", customer_id, store["store_id"], rng.choice(["app", "app", "wechat", "sms"]),
                            "campaign", _iso(exposed_dt), max(1, int(round(basket / 90.0))), gross,
                        ]
                    )
                    exposure_rows += 1
                if rng.random() < 0.30:
                    # Non-converting / pre-existing regular exposure.
                    exposed_dt = ordered_dt - timedelta(hours=rng.uniform(1.0, 72.0))
                    exposure_seq += 1
                    exposure_writer.writerow(
                        [
                            f"E{exposure_seq:08d}", customer_id, store["store_id"],
                            _weighted(rng, CHANNELS, [0.25, 0.15, 0.20, 0.20, 0.20]),
                            "regular", _iso(exposed_dt), 1, gross,
                        ]
                    )
                    exposure_rows += 1

                if status not in ("completed", "shipped"):
                    continue
                refund_prob = BASE_REFUND_RATE
                if promo_day:
                    refund_prob = PROMO_REFUND_RATE
                elif store["store_group"] == "control" and in_campaign:
                    refund_prob = CONTROL_REFUND_RATE
                if rng.random() >= refund_prob:
                    continue
                # Refund delay: most are quick, but a real tail lands after the
                # analysis cutoff -- especially on late-campaign orders.
                roll = rng.random()
                if roll < 0.65:
                    delay_days = rng.uniform(0.5, 14.0)
                elif roll < 0.85:
                    delay_days = rng.uniform(14.0, 30.0)
                else:
                    delay_days = rng.uniform(30.0, 62.0)
                refunded_dt = ordered_dt + timedelta(days=delay_days)
                amount = max(1, int(round(net * rng.uniform(0.40, 1.0))))
                refunded_orders += 1
                if refunded_dt <= cutoff:
                    refund_init_seq += 1
                    refund_initial_writer.writerow(
                        [f"R{refund_init_seq:07d}", order_id, customer_id, store["store_id"], _iso(refunded_dt), amount]
                    )
                else:
                    refund_back_seq += 1
                    late_refund_orders += 1
                    refund_backfill_writer.writerow(
                        [f"RB{refund_back_seq:07d}", order_id, customer_id, store["store_id"], _iso(refunded_dt), amount]
                    )

            # Ongoing regular exposures independent of orders (all stores).
            for _ in range(1 if rng.random() < 0.55 else 0):
                member_id = rng.choice(member_ids)
                exposed_dt = day_base + timedelta(seconds=rng.randrange(7 * 3600, 22 * 3600))
                exposure_seq += 1
                exposure_writer.writerow(
                    [
                        f"E{exposure_seq:08d}", member_id, store["store_id"],
                        _weighted(rng, CHANNELS, CHANNEL_WEIGHTS), "regular", _iso(exposed_dt),
                        1, int(round(_basket_mean(store) * 100)),
                    ]
                )
                exposure_rows += 1
            # Campaign exposures that did not convert.
            if promo_day and rng.random() < 0.7:
                for _ in range(rng.randint(1, 2)):
                    member_id = rng.choice(member_ids)
                    exposed_dt = day_base + timedelta(seconds=rng.randrange(7 * 3600, 21 * 3600))
                    exposure_seq += 1
                    exposure_writer.writerow(
                        [
                            f"E{exposure_seq:08d}", member_id, store["store_id"],
                            rng.choice(["app", "app", "wechat", "sms"]), "campaign", _iso(exposed_dt),
                            1, int(round(_basket_mean(store) * 100)),
                        ]
                    )
                    exposure_rows += 1

    # A handful of deliberately invalid rows, to prove bad input is quarantined
    # rather than silently joining the analysis.
    order_writer.writerow(["O-BAD-1", "C-BAD", "S001", "app", "2026-07-01T10:00:00Z", "completed", 10000, 2000, 7000])
    order_writer.writerow(["O-BAD-2", "C-BAD", "S001", "carrier-pigeon", "2026-07-01T10:00:00Z", "completed", 10000, 2000, 8000])
    exposure_writer.writerow(["E-BAD-1", "C-BAD", "S001", "app", "campaign", "not-a-time", 1, 1000])

    for fh in (order_file, exposure_file, refund_initial_file, refund_backfill_file):
        fh.close()

    promo = [s for s in stores if s["store_group"] == "promo"]
    control = [s for s in stores if s["store_group"] == "control"]
    truth = {
        "seed": seed,
        "design": {
            "campaign_start": CAMPAIGN_START.isoformat(),
            "campaign_end": CAMPAIGN_END.isoformat(),
            "pre_start": PRE_START.isoformat(),
            "pre_end": PRE_END.isoformat(),
            "refund_cutoff": REFUND_CUTOFF,
            "true_order_rate_lift": TRUE_ORDER_LIFT,
            "promo_refund_rate": PROMO_REFUND_RATE,
            "base_refund_rate": BASE_REFUND_RATE,
            "selection": "promo assignment depends on size, traffic, urban score, membership and format",
        },
        "counts": {
            "stores": len(stores),
            "promo_stores": len(promo),
            "control_stores": len(control),
            "orders": order_seq,
            "pre_orders": pre_orders,
            "campaign_orders": campaign_orders,
            "exposures": exposure_rows,
            "refunded_orders": refunded_orders,
            "late_refund_orders": late_refund_orders,
        },
    }
    (output_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return truth


def seed_demo(data_dir: Path, db_path: Path, order_scale: float = 1.0, reset: bool = False) -> dict[str, Any]:
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    if reset:
        if data_dir.exists():
            shutil.rmtree(data_dir)
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(db_path) + suffix)
            if target.exists():
                target.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    truth = generate(data_dir, order_scale=order_scale)
    init_db(db_path)
    seed_versions(db_path)
    ingests = []
    for table, filename in (
        ("stores", "stores.csv"),
        ("orders", "orders.csv"),
        ("exposure", "exposure.csv"),
        ("refunds", "refunds_initial.csv"),
    ):
        ingests.append(ingest_csv(db_path, table, data_dir / filename))
    snapshot = create_snapshot(db_path)
    return {"truth": truth, "ingests": ingests, "snapshot": snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 PromoLens 合成数据并装载演示数据库")
    parser.add_argument("--data-dir", default="var/data")
    parser.add_argument("--db", default="var/promolens.db")
    parser.add_argument("--order-scale", type=float, default=1.0)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    result = seed_demo(Path(args.data_dir), Path(args.db), args.order_scale, args.reset)
    print(json.dumps({
        "orders": result["truth"]["counts"]["orders"],
        "stores": result["truth"]["counts"]["stores"],
        "promo": result["truth"]["counts"]["promo_stores"],
        "control": result["truth"]["counts"]["control_stores"],
        "late_refunds": result["truth"]["counts"]["late_refund_orders"],
        "snapshot": result["snapshot"]["snapshot_hash"][:16],
        "rows": result["snapshot"]["row_counts"],
        "rejected": sum(i["rejected"] for i in result["ingests"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
