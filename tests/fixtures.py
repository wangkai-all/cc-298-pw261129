"""Shared fixtures: one small deterministic dataset for the whole test run.

The dataset is generated with the same generator as the demo but at 35% order
volume, so the suite stays fast while keeping the design properties that matter
(non-random grouping, late refunds, a few invalid rows).
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from promolens import reports
from promolens.db import init_db
from promolens.ingest import create_snapshot, ingest_csv
from promolens.server import State, make_handler
from promolens.seed import generate
from promolens.versions import seed_versions


@dataclass
class Env:
    root: Path
    data_dir: Path
    db_path: Path
    truth: dict


_ENV: Env | None = None


def get_env() -> Env:
    """Build the dataset once, on first use."""
    global _ENV
    if _ENV is not None:
        return _ENV
    root = Path(tempfile.mkdtemp(prefix="promolens-tests-"))
    data_dir = root / "data"
    db_path = root / "test.db"
    truth = generate(data_dir, order_scale=0.35)
    init_db(db_path)
    seed_versions(db_path)
    for table, filename in (
        ("stores", "stores.csv"),
        ("orders", "orders.csv"),
        ("exposure", "exposure.csv"),
        ("refunds", "refunds_initial.csv"),
    ):
        ingest_csv(db_path, table, data_dir / filename)
    create_snapshot(db_path)
    _ENV = Env(root=root, data_dir=data_dir, db_path=db_path, truth=truth)
    return _ENV


def drain(timeout: float = 180.0) -> None:
    reports.drain(get_env().db_path, timeout=timeout)


def enqueue(**kwargs) -> dict:
    env = get_env()
    report = reports.enqueue_report(env.db_path, **kwargs)
    drain()
    return reports.get_report(env.db_path, report["report_id"])


class ServerFixture(unittest.TestCase):
    """Boots one HTTP server for all API tests in the process."""

    server = None
    state = None
    base = ""

    @classmethod
    def setUpClass(cls) -> None:
        if ServerFixture.server is not None:
            cls.base = ServerFixture.base
            return
        env = get_env()
        state = State(str(env.db_path), env.data_dir)
        state.start_worker()
        import http.server

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ServerFixture.state = state
        ServerFixture.server = server
        ServerFixture.base = f"http://127.0.0.1:{server.server_address[1]}"
        cls.base = ServerFixture.base


def wait_until(fn, timeout: float = 60.0, interval: float = 0.1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待超时；最后一次结果：{last!r}")
