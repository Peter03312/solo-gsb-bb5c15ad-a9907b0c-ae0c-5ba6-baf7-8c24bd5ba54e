"""HTTP API 集成测试：python tests/test_http_api.py

通过 wsgiref 风格直接在子线程启动真实 HTTP 服务（临时数据库、随机端口），
覆盖：
- 关系间隔（最短/最长等待）的保存、推算、持久化；
- 非法间隔输入一律 400 且记录不被修改；
- 成环冲突给出相反依据链且不得定稿；
- base_version 过期沿用 409，不覆盖较新版本；
- 版本回看/恢复保留每条关系当时的间隔设置；
- 暂存事件关联关系跳过推算并给出原因。
"""
import json
import pathlib
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402
import server  # noqa: E402


def start_server():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    server.DB_PATH = tmp.name
    db.init_db(server.DB_PATH)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return httpd, "http://127.0.0.1:{}".format(httpd.server_address[1])


def call(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ev(key, name=None, es="2026-10-01T10:00", ls="2026-10-01T12:00", dur=10):
    return {"key": key, "name": name or key.upper(), "earliest_start": es,
            "latest_start": ls, "duration_minutes": dur, "note": ""}


def rel(a, b, min_wait=0, max_wait=None):
    return {"predecessor_key": a, "successor_key": b,
            "min_wait_minutes": min_wait, "max_wait_minutes": max_wait}


def save(base, url, rid, events, relations, title="T"):
    return call("PUT", "{}/api/reviews/{}".format(url, rid),
                {"base_version": base, "title": title,
                 "events": events, "relations": relations})


def test_waits_save_and_compute():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        status, d = save(1, url, rid,
                         [ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                          ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30)],
                         [rel("a", "b", 10, 60)])
        assert status == 200
        assert d["relations"][0]["min_wait_minutes"] == 10
        assert d["relations"][0]["max_wait_minutes"] == 60
        bid = str(d["events"][1]["id"])
        assert d["computation"]["result"]["windows"][bid]["earliest_start"] == "2026-10-01T10:25"
    finally:
        httpd.shutdown()


def test_illegal_waits_rejected_without_touching_record():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        status, d = save(1, url, rid, [ev("a"), ev("b")], [rel("a", "b", 10, 60)])
        assert status == 200 and d["review"]["version"] == 2

        cases = [
            ([ev("a"), ev("b")], rel("a", "b", 50, 10)),          # max < min
            ([ev("a"), ev("b")], rel("a", "b", -1, None)),        # 负数
            ([ev("a"), ev("b")], rel("a", "b", 2.5, None)),       # 小数
            ([ev("a"), ev("b")], rel("a", "b", 0, "x")),          # 非整数
            ([ev("a"), ev("b")], rel("a", "b", 0, -5)),           # 负最长
            ([ev("a"), ev("b")], rel("a", "b", True, None)),      # 布尔
        ]
        for events, r in cases:
            status, d = save(2, url, rid, events, [r], title="非法")
            assert status == 400, (status, d, r)
        # 记录未改：仍为 v2、标题未变、间隔仍为 10/60
        _, bundle = call("GET", "{}/api/reviews/{}".format(url, rid))
        assert bundle["review"]["version"] == 2
        assert bundle["review"]["title"] == "T"
        assert bundle["relations"][0]["min_wait_minutes"] == 10
        assert bundle["relations"][0]["max_wait_minutes"] == 60
    finally:
        httpd.shutdown()


def test_cycle_conflict_opposite_chains_and_no_finalize():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        status, d = save(1, url, rid,
                         [ev("a", "A", "2026-10-01T10:00", "2026-10-01T23:00", 30),
                          ev("b", "B", "2026-10-01T10:00", "2026-10-01T23:00", 30)],
                         [rel("a", "b", 15), rel("b", "a", 15)])
        assert status == 200
        result = d["computation"]["result"]
        assert result["status"] == "conflict"
        cyc = next(c for c in result["conflicts"] if c["type"] == "cycle")
        assert set(cyc["event_ids"]) == {e["id"] for e in d["events"]}
        assert cyc["forward_chain"] and cyc["reverse_chain"]
        status, _ = call("POST", "{}/api/reviews/{}/finalize".format(url, rid),
                         {"base_version": 2})
        assert status == 400
    finally:
        httpd.shutdown()


def test_stale_version_409_keeps_newer():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        status, d = save(1, url, rid, [ev("a")], [])
        assert status == 200 and d["review"]["version"] == 2
        status, d = save(1, url, rid, [ev("a")], [])  # 过期 base
        assert status == 409 and d["current_version"] == 2
        _, bundle = call("GET", "{}/api/reviews/{}".format(url, rid))
        assert bundle["review"]["version"] == 2
    finally:
        httpd.shutdown()


def test_version_review_and_restore_keep_waits():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        save(1, url, rid, [ev("a"), ev("b")], [rel("a", "b", 10, 60)])          # v2
        save(2, url, rid, [ev("a"), ev("b")], [rel("a", "b", 0, None)])        # v3 改掉间隔

        status, v2 = call("GET", "{}/api/reviews/{}/versions/2".format(url, rid))
        assert status == 200
        assert v2["input"]["relations"][0]["min_wait_minutes"] == 10
        assert v2["input"]["relations"][0]["max_wait_minutes"] == 60

        status, d = call("POST", "{}/api/reviews/{}/versions/2/restore".format(url, rid),
                         {"base_version": 3})
        assert status == 200
        assert d["review"]["version"] == 4 and d["review"]["status"] == "draft"
        assert d["relations"][0]["min_wait_minutes"] == 10
        assert d["relations"][0]["max_wait_minutes"] == 60

        # 过期恢复 -> 409
        status, d = call("POST", "{}/api/reviews/{}/versions/2/restore".format(url, rid),
                         {"base_version": 3})
        assert status == 409 and d["current_version"] == 4
    finally:
        httpd.shutdown()


def test_draft_relation_skipped_with_reason():
    httpd, url = start_server()
    try:
        _, d = call("POST", url + "/api/reviews", {"title": "T"})
        rid = d["review"]["id"]
        draft_ev = dict(ev("b", "B"), earliest_start=None, latest_start=None)
        status, d = save(1, url, rid, [ev("a"), draft_ev], [rel("a", "b", 10, 30)])
        assert status == 200
        result = d["computation"]["result"]
        assert len(result["drafts"]) == 1
        assert len(result["skipped_relations"]) == 1
        s = result["skipped_relations"][0]
        assert s["min_wait_minutes"] == 10 and s["max_wait_minutes"] == 30
        assert "暂存" in s["reason"]
        # 有暂存事件不得定稿
        status, _ = call("POST", "{}/api/reviews/{}/finalize".format(url, rid),
                         {"base_version": 2})
        assert status == 400
    finally:
        httpd.shutdown()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
