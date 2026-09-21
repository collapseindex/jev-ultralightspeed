"""Controlled burst-sensitive HTTP server; not a model or live API benchmark."""

import hashlib
import json
import random
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Anchored to the file, not to where you happen to be standing.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_ultralightspeed import Client  # noqa: E402


def measure_throttle(size, rounds, seed, transport):
    if size < 1 or rounds < 1:
        raise ValueError("items and rounds must be positive")
    items = [f"message {i}: café 東京" for i in range(size)]
    question = "Does this need attention?"
    samples = {False: [], True: []}
    rng = random.Random(seed)
    expected = None

    def run(paced):
        recent, accepted, attempts = deque(), [], []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def send(self, status, data):
                payload = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if status == 429:
                    self.send_header("Retry-After", "0.2")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.send(200, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    now = time.monotonic()
                    attempts.append(now)
                    while recent and now - recent[0] >= 0.2:
                        recent.popleft()
                    limited = len(recent) >= 5
                    if not limited:
                        recent.append(now)
                        accepted.append(json.dumps(body, sort_keys=True))
                if limited:
                    self.send(429, {"detail": "five requests per rolling 200ms"})
                    return
                answers = {name: {"noul": hashlib.sha256(text.encode()).digest()[0] / 255}
                           for name, text in body["state"].items()}
                self.send(200, {"answers": answers})

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/systemone"
            with Client(key="local-only", url=url, pack=8, workers=8, cache=False,
                        transport=transport, requests_per_minute=1500, paced=paced) as client:
                client.warm()
                start = time.perf_counter()
                answers = client.classify(items, question)
                elapsed = time.perf_counter() - start
                signature = ([asdict(answer) for answer in answers], sorted(accepted))
                return elapsed, client.usage.retries, len(attempts), signature
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    # Warm each arm first; every measured arm gets a fresh server and client.
    for turn in range(rounds + 1):
        order = [False, True]
        rng.shuffle(order)
        for paced in order:
            elapsed, retries, attempts, signature = run(paced)
            if expected is None:
                expected = signature
            if signature != expected:
                raise AssertionError("request bodies or full answers differ between arms")
            if turn:
                samples[paced].append((elapsed, retries, attempts))
    print("Loopback HTTP/1.1: the http2 option exercises httpx fallback, not H2 multiplexing.")
    print(f"LOCAL ONLY: {size} items, {rounds} rounds, 5 requests/200ms server")
    print("pack=8 workers=8 RPM=1500; request bodies and full answers agree: 100%")
    print(f"{'mode':<10}{'p50 s':>10}{'p95 s':>10}{'items/s':>12}{'retries':>10}{'attempts':>10}")
    for paced, rows in samples.items():
        times = sorted(row[0] for row in rows)
        median = statistics.median(times)
        tail = times[min(len(times) - 1, int(0.95 * len(times)))]
        retry = statistics.median(row[1] for row in rows)
        attempts = statistics.median(row[2] for row in rows)
        name = "paced" if paced else "burst"
        print(f"{name:<10}{median:>10.3f}{tail:>10.3f}{size / median:>12.1f}"
              f"{retry:>10.0f}{attempts:>10.0f}")
    return 0
