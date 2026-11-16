import argparse

from .seed import seed_demo
from .server import serve


def main() -> int:
    parser = argparse.ArgumentParser(description="PromoLens 促销对照分析应用")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("serve", help="启动网页应用")
    run.add_argument("--db", default="var/promolens.db")
    run.add_argument("--data-dir", default="var/data")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8080)
    seed = sub.add_parser("seed", help="生成合成数据并装载演示库")
    seed.add_argument("--db", default="var/promolens.db")
    seed.add_argument("--data-dir", default="var/data")
    seed.add_argument("--reset", action="store_true")
    seed.add_argument("--order-scale", type=float, default=1.0)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.db, args.data_dir, args.host, args.port)
        return 0
    if args.command == "seed":
        seed_demo(args.data_dir, args.db, args.order_scale, args.reset)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
