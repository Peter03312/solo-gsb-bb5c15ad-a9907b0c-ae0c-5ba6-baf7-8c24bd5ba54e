"""推算引擎单元测试：python tests/test_compute.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import compute  # noqa: E402


def ev(i, name, es, ls, dur):
    return {"id": i, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur}


def rel(i, a, b, min_wait=0, max_wait=None):
    return {"id": i, "predecessor_id": a, "successor_id": b,
            "min_wait_minutes": min_wait, "max_wait_minutes": max_wait}


def test_basic_propagation():
    events = [
        ev(1, "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok", res
    w2 = res["windows"][2]
    # 响应最早开始 = 告警最早开始 10:00 + 15 分钟
    assert w2["earliest_start"] == "2026-10-01T10:15", w2
    w1 = res["windows"][1]
    # 告警最晚开始 = 响应最晚开始 12:00 - 15 分钟（输入 10:30 更紧，保持 10:30）
    assert w1["latest_start"] == "2026-10-01T10:30", w1
    # 依据链应包含输入与关系两个环节
    assert len(w2["earliest_chain"]) == 2
    assert "告警" in w2["earliest_chain"][0]["text"]


def test_backward_propagation():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 60),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T11:00", 10),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok", res
    # A 最晚开始 = B 最晚开始 11:00 - 60 分钟 = 10:00
    assert res["windows"][1]["latest_start"] == "2026-10-01T10:00"


def test_window_conflict():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:10", 60),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T10:30", 5),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "conflict"
    win_conflicts = [c for c in res["conflicts"] if c["type"] == "window"]
    # A 的最晚可行开始被回推到 09:30（< 最早 10:00），B 的最早可行开始 11:00 > 最晚 10:30
    assert {c["event_ids"][0] for c in win_conflicts} == {1, 2}
    assert res["windows"][2]["earliest_start"] == "2026-10-01T11:00"
    c2 = next(c for c in win_conflicts if c["event_ids"] == [2])
    assert c2["earliest_chain"] and c2["latest_chain"]


def test_cycle_conflict():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2), rel(2, 2, 1)])
    assert res["status"] == "conflict"
    assert any(c["type"] == "cycle" for c in res["conflicts"])


def test_invalid_input_range():
    events = [ev(1, "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)]
    res = compute.compute(events, [])
    assert res["status"] == "conflict"
    assert res["conflicts"][0]["type"] == "invalid_range"


def test_draft_event_skips_relation():
    events = [
        ev(1, "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        ev(2, "缺范围", None, None, 10),
        ev(3, "缺时长", "2026-10-01T10:00", "2026-10-01T11:00", None),
    ]
    res = compute.compute(events, [rel(1, 1, 2), rel(2, 2, 3)])
    assert res["status"] == "ok"
    assert len(res["drafts"]) == 2
    assert len(res["skipped_relations"]) == 2
    assert set(res["windows"].keys()) == {1}
    assert "暂存" in res["drafts"][0]["reason"]


def test_empty():
    res = compute.compute([], [])
    assert res["status"] == "ok" and res["windows"] == {}


def test_min_wait_forward_propagation():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T12:00", 10),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T13:00", 5),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=20)])
    assert res["status"] == "ok", res
    # B 最早开始 = 10:00 + 10(持续) + 20(最短等待) = 10:30
    assert res["windows"][2]["earliest_start"] == "2026-10-01T10:30"
    # A 最晚开始 = 13:00 − 20 − 10 = 12:30（输入 12:00 更紧，保持 12:00）
    assert res["windows"][1]["latest_start"] == "2026-10-01T12:00"
    assert "至少等待 20 分钟" in res["windows"][2]["earliest_chain"][-1]["text"]


def test_min_wait_backward_propagation():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 60),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 10),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=30)])
    assert res["status"] == "ok", res
    # A 最晚开始 = B 最晚 12:00 − 60(持续) − 30(最短等待) = 10:30
    assert res["windows"][1]["latest_start"] == "2026-10-01T10:30"


def test_max_wait_backward_propagation():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 10),
        ev(2, "B", "2026-10-01T12:00", "2026-10-01T13:00", 5),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=0, max_wait=20)])
    assert res["status"] == "ok", res
    # B 最晚开始 = A 最晚 23:00 + 10(持续) + 20(最长等待) = 23:30；
    # 输入 13:00 更紧，保持 13:00
    assert res["windows"][2]["latest_start"] == "2026-10-01T13:00"
    # A 最早开始 = B 最早 12:00 − 10(持续) − 20(最长等待) = 11:30
    assert res["windows"][1]["earliest_start"] == "2026-10-01T11:30"


def test_max_wait_propagation_binds():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 10),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T23:00", 5),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=10, max_wait=50)])
    assert res["status"] == "ok", res
    # B 最早 = A 最早 10:00 + 10 + 10 = 10:20
    assert res["windows"][2]["earliest_start"] == "2026-10-01T10:20"
    # B 最晚 = A 最晚 23:00 + 10 + 50 = 24:00；输入 23:00 更紧
    assert res["windows"][2]["latest_start"] == "2026-10-01T23:00"
    # A 最晚 = B 最晚 23:00 − 10 − 10 = 22:40
    assert res["windows"][1]["latest_start"] == "2026-10-01T22:40"
    # A 最早 = B 最早 10:20 − 10 − 50 = 09:20（输入 10:00 更紧）
    assert res["windows"][1]["earliest_start"] == "2026-10-01T10:00"


def test_max_wait_conflict():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:00", 60),
        ev(2, "B", "2026-10-01T12:00", "2026-10-01T12:00", 5),
    ]
    # 间隔 = B 开始 12:00 − A 结束 11:00 = 60 分钟，但最长只允许 30 分钟
    res = compute.compute(events, [rel(1, 1, 2, min_wait=0, max_wait=30)])
    assert res["status"] == "conflict", res
    # A 最早被后向（最长等待前向）推到 12:00 − 60 − 30 = 10:30，超过 A 最晚 10:00
    win = [c for c in res["conflicts"] if c["type"] == "window"]
    assert {c["event_ids"][0] for c in win} == {1, 2}


def test_cycle_with_positive_wait():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 30),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T23:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=15), rel(2, 2, 1, min_wait=15)])
    assert res["status"] == "conflict"
    cyc = next(c for c in res["conflicts"] if c["type"] == "cycle")
    assert set(cyc["event_ids"]) == {1, 2}
    # 相反方向的两条依据链都应给出
    assert cyc["forward_chain"] and cyc["reverse_chain"]
    assert len(cyc["forward_chain"]) == 2 and len(cyc["reverse_chain"]) == 2
    # 前向链给出开始时刻之差的下界，后向链反向回推；两条链方向相反
    assert "≥ 45 分钟" in cyc["forward_chain"][0]["text"]
    assert "后向回推" in cyc["reverse_chain"][0]["text"]


def test_zero_wait_cycle_is_feasible():
    # 环上总等待为 0（且持续为 0）时恰可满足，不应报环冲突
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    res = compute.compute(events, [rel(1, 1, 2), rel(2, 2, 1)])
    assert res["status"] == "ok", res["conflicts"]


def test_legacy_relation_without_wait_fields():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    # 旧格式关系：没有任何间隔字段，等价 min=0/max=None
    old_rel = {"id": 9, "predecessor_id": 1, "successor_id": 2}
    res = compute.compute(events, [old_rel])
    assert res["status"] == "ok"
    assert res["windows"][2]["earliest_start"] == "2026-10-01T10:15"
    assert res["skipped_relations"] == []


def test_draft_relation_keeps_waits_when_skipped():
    events = [
        ev(1, "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        ev(2, "暂存", None, None, 10),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_wait=15, max_wait=60)])
    assert len(res["skipped_relations"]) == 1
    s = res["skipped_relations"][0]
    assert s["min_wait_minutes"] == 15 and s["max_wait_minutes"] == 60
    assert "暂存" in s["reason"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
