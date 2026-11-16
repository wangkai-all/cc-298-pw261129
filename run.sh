#!/usr/bin/env bash
# 启动 PromoLens：首次运行会生成合成数据并装载演示库。
set -euo pipefail
cd "$(dirname "$0")"

DB="${PROMOLENS_DB:-var/promolens.db}"
DATA="${PROMOLENS_DATA:-var/data}"
HOST="${PROMOLENS_HOST:-127.0.0.1}"
PORT="${PROMOLENS_PORT:-8080}"

if [[ "${1:-}" == "--reset" ]]; then
  echo "重置演示数据…"
  python3 -m promolens seed --db "$DB" --data-dir "$DATA" --reset
  shift
elif [[ ! -f "$DB" || ! -f "$DATA/refunds_backfill.csv" ]]; then
  echo "未找到演示库，正在生成合成数据（约 40 秒）…"
  python3 -m promolens seed --db "$DB" --data-dir "$DATA"
fi

exec python3 -m promolens serve --db "$DB" --data-dir "$DATA" --host "$HOST" --port "$PORT"
