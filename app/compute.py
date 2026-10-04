"""时间窗推算引擎。

输入：事件（起始时间范围 + 持续分钟数）与“先于”关系。A 先于 B 表示
A 必须在 B 开始前结束，并可附带“前项结束 → 后项开始”的允许间隔（分钟）：

  - min_wait_minutes：最短等待（非负整数，缺省 0），B.start ≥ A.start + A.duration + min
  - max_wait_minutes：最长等待（非负整数或 None；None 表示不限），
                      B.start ≤ A.start + A.duration + max

未设置间隔的旧关系等价于 min=0、max=None，即“A 结束不晚于 B 开始”的旧语义。

输出：每个完整事件的可行开始时间窗（最早/最晚可行开始）及推算依据链；
约束冲突（窗口不可满足、关系成环、输入范围自相矛盾）时给出冲突事件、
冲突类型与前向/后向（相反方向）依据链；缺少时间范围的事件作为“暂存”
处理，其关联关系不参与推算并标明原因。
"""

from datetime import datetime, timedelta


def _parse(s):
    return datetime.fromisoformat(s)


def _fmt(dt):
    return dt.isoformat(timespec="minutes")


def _norm_waits(r):
    """读取关系上的间隔设置，缺省按旧语义 min=0 / max=None。"""
    minw = r.get("min_wait_minutes")
    minw = int(minw) if minw is not None else 0
    maxw = r.get("max_wait_minutes")
    maxw = int(maxw) if maxw is not None else None
    return minw, maxw


def _wait_paren(minw, maxw):
    """间隔设置的简短说明，作为关系描述的括注。"""
    if maxw is None:
        return "" if minw == 0 else "（至少等待 {} 分钟）".format(minw)
    if maxw == minw:
        return "（须恰好等待 {} 分钟）".format(minw)
    if minw == 0:
        return "（至多等待 {} 分钟）".format(maxw)
    return "（须等待 {}～{} 分钟）".format(minw, maxw)


def compute(events, relations):
    """推算时间窗。

    events:   [{"id", "name", "earliest_start", "latest_start", "duration_minutes"}]
              时间字段为 ISO 字符串或 None；duration_minutes 为整数或 None。
    relations: [{"id", "predecessor_id", "successor_id",
                 "min_wait_minutes"?, "max_wait_minutes"?}]

    返回 dict：
      status            "ok" | "conflict"
      windows           {event_id: {earliest_start, latest_start, earliest_end,
                                    latest_end, earliest_chain, latest_chain}}
      conflicts         [{type, event_ids, message, ...}]
      drafts            [{id, name, reason}]            暂存事件（不参与推算）
      skipped_relations [{id, predecessor_id, successor_id,
                          min_wait_minutes, max_wait_minutes, reason}]
    """
    by_id = {e["id"]: e for e in events}

    # ---- 1. 区分完整事件与暂存事件 -------------------------------------
    drafts = []
    active = {}
    for e in events:
        missing = []
        if not e.get("earliest_start"):
            missing.append("最早开始时间")
        if not e.get("latest_start"):
            missing.append("最晚开始时间")
        if e.get("duration_minutes") is None:
            missing.append("持续分钟数")
        if missing:
            drafts.append({
                "id": e["id"],
                "name": e["name"],
                "reason": "缺少" + "、".join(missing) + "，已暂存，不参与本次推算",
            })
        else:
            active[e["id"]] = e

    # ---- 2. 过滤涉及暂存事件的关系，并规范化间隔设置 --------------------
    skipped_relations = []
    active_relations = []
    for r in relations:
        minw, maxw = _norm_waits(r)
        rr = {
            "id": r["id"],
            "predecessor_id": r["predecessor_id"],
            "successor_id": r["successor_id"],
            "min_wait_minutes": minw,
            "max_wait_minutes": maxw,
        }
        a, b = rr["predecessor_id"], rr["successor_id"]
        if a not in by_id or b not in by_id:
            skipped_relations.append({
                **rr,
                "reason": "关系引用了不存在的事件，已忽略",
            })
            continue
        inactive = [by_id[x]["name"] for x in (a, b) if x not in active]
        if inactive:
            skipped_relations.append({
                **rr,
                "reason": "关联事件「" + "、".join(inactive)
                          + "」处于暂存状态，该“先于”关系暂不参与推算",
            })
        else:
            active_relations.append(rr)

    est = {i: _parse(e["earliest_start"]) for i, e in active.items()}
    lst = {i: _parse(e["latest_start"]) for i, e in active.items()}
    dur = {i: int(e["duration_minutes"]) for i, e in active.items()}
    # 依据来源：src[x] = (other, relation_id, rule) 表示 x 的边界由 other 经
    # rule（emin/emax/lmin/lmax 四种传播之一）推得。
    est_src = {i: None for i in active}
    lst_src = {i: None for i in active}

    conflicts = []
    invalid_range = set()
    for i, e in active.items():
        if est[i] > lst[i]:
            invalid_range.add(i)
            conflicts.append({
                "type": "invalid_range",
                "event_ids": [i],
                "message": "事件「{}」输入的时间范围自相矛盾：最早开始 {} 晚于最晚开始 {}".format(
                    e["name"], _fmt(est[i]), _fmt(lst[i])),
            })

    rel_by_id = {r["id"]: r for r in active_relations}

    # ---- 3. 约束传播（Bellman-Ford 方式，同时施加间隔上下界） -----------
    # 对关系 A 先于 B（A 持续 da，最短等待 lo，最长等待 hi），一次扫描施加：
    #   最短间隔前向：est[B] ≥ est[A] + da + lo
    #   最短间隔后向：lst[A] ≤ lst[B] − da − lo
    #   最长间隔后向：lst[B] ≤ lst[A] + da + hi   （hi 存在时）
    #   最长间隔前向：est[A] ≥ est[B] − da − hi   （hi 存在时）
    def tighten():
        changed = False
        for r in active_relations:
            a, b = r["predecessor_id"], r["successor_id"]
            lo, hi = r["min_wait_minutes"], r["max_wait_minutes"]
            da = timedelta(minutes=dur[a])

            cand = est[a] + da + timedelta(minutes=lo)
            if cand > est[b]:
                est[b] = cand
                est_src[b] = (a, r["id"], "emin")
                changed = True

            cand = lst[b] - da - timedelta(minutes=lo)
            if cand < lst[a]:
                lst[a] = cand
                lst_src[a] = (b, r["id"], "lmin")
                changed = True

            if hi is not None:
                cand = lst[a] + da + timedelta(minutes=hi)
                if cand < lst[b]:
                    lst[b] = cand
                    lst_src[b] = (a, r["id"], "lmax")
                    changed = True

                cand = est[b] - da - timedelta(minutes=hi)
                if cand > est[a]:
                    est[a] = cand
                    est_src[a] = (b, r["id"], "emax")
                    changed = True
        return changed

    n = len(active)
    has_cycle = False
    if n:
        for _ in range(n):
            if not tighten():
                break
        else:
            # 第 n 轮仍可收紧：差约束中存在正环，即“先于”关系成环且间隔/持续
            # 要求沿环累加后不可满足。
            has_cycle = True

    # ---- 3b. 定位成环事件 ------------------------------------------------
    def src_cycle_nodes(src):
        """沿 src 依据指针找出仍在收紧的环上的节点集合。"""
        for start in active:
            seen = set()
            cur = start
            while cur is not None and cur not in seen:
                seen.add(cur)
                s = src.get(cur)
                cur = s[0] if s else None
            if cur is not None:
                nodes = [cur]
                s = src.get(cur)
                nxt = s[0] if s else None
                guard = 0
                while nxt is not None and nxt != cur and guard <= n:
                    nodes.append(nxt)
                    s2 = src.get(nxt)
                    nxt = s2[0] if s2 else None
                    guard += 1
                return nodes
        return []

    def order_along_relations(node_set):
        """在环节点诱导子图中按“先于”方向 DFS，返回有序节点与逐条关系 id。"""
        node_set = set(node_set)
        out = {i: [] for i in node_set}
        for r in active_relations:
            a, b = r["predecessor_id"], r["successor_id"]
            if a in node_set and b in node_set:
                out[a].append((b, r["id"]))

        def dfs(u, stack, edges, on):
            on.add(u)
            stack.append(u)
            for v, rid in out.get(u, []):
                if v in on:
                    idx = stack.index(v)
                    return stack[idx:], edges[idx:] + [rid]
                res = dfs(v, stack, edges + [rid], on)
                if res:
                    return res
            on.discard(u)
            stack.pop()
            return None

        for s in node_set:
            res = dfs(s, [], [], set())
            if res:
                return res
        return [], []

    cyc_nodes = []
    cyc_edges = []
    if has_cycle:
        node_set = src_cycle_nodes(est_src) or src_cycle_nodes(lst_src)
        cyc_nodes, cyc_edges = order_along_relations(node_set)

    # ---- 4. 依据链构建 ---------------------------------------------------
    def step_text(cur, src, kind):
        other, rid, rule = src
        r = rel_by_id[rid]
        a, b = r["predecessor_id"], r["successor_id"]
        an, bn = active[a]["name"], active[b]["name"]
        da = dur[a]
        paren = _wait_paren(r["min_wait_minutes"], r["max_wait_minutes"])
        if rule == "emin":
            return "「{}」先于「{}」{}，且「{}」持续 {} 分钟 ⇒ 「{}」最早开始不早于 {}".format(
                an, bn, paren, an, da, bn, _fmt(est[b]))
        if rule == "emax":
            return ("「{}」先于「{}」{}，「{}」最早开始不早于 {}，且「{}」持续 {} 分钟"
                    " ⇒ 「{}」最早开始不早于 {}").format(
                an, bn, paren, bn, _fmt(est[b]), an, da, an, _fmt(est[a]))
        if rule == "lmin":
            return "「{}」先于「{}」{}，且「{}」持续 {} 分钟 ⇒ 「{}」最晚开始不晚于 {}".format(
                an, bn, paren, an, da, an, _fmt(lst[a]))
        return "「{}」先于「{}」{}，「{}」最晚开始不晚于 {}，且「{}」持续 {} 分钟 ⇒ 「{}」最晚开始不晚于 {}".format(
            an, bn, paren, an, _fmt(lst[a]), an, da, bn, _fmt(lst[b]))

    def build_chain(start_id, kind, src_map):
        """从结论回溯到输入依据，返回按“输入 → 结论”排列的步骤列表。"""
        steps = []
        cur = start_id
        seen = set()
        while True:
            ev = active[cur]
            src = src_map[cur]
            val = est[cur] if kind == "est" else lst[cur]
            if src is None:
                label = "最早开始" if kind == "est" else "最晚开始"
                steps.append({"text": "「{}」输入的{}为 {}".format(ev["name"], label, _fmt(val))})
                break
            steps.append({"text": step_text(cur, src, kind)})
            if cur in seen:
                steps.append({"text": "（检测到循环依赖，回溯终止）"})
                break
            seen.add(cur)
            cur = src[0]
        steps.reverse()
        return steps

    # ---- 5. 汇总每个事件的可行时间窗 ------------------------------------
    windows = {}
    for i in active:
        windows[i] = {
            "earliest_start": _fmt(est[i]),
            "latest_start": _fmt(lst[i]),
            "earliest_end": _fmt(est[i] + timedelta(minutes=dur[i])),
            "latest_end": _fmt(lst[i] + timedelta(minutes=dur[i])),
            "earliest_chain": build_chain(i, "est", est_src),
            "latest_chain": build_chain(i, "lst", lst_src),
        }

    # ---- 6. 成环冲突：冲突事件 + 前向/后向（相反）依据链 -----------------
    if has_cycle:
        names = [active[i]["name"] for i in cyc_nodes]
        forward_chain = []
        reverse_chain = []
        if cyc_nodes:
            k = len(cyc_nodes)

            def edge_parts(rid, a, b):
                r = rel_by_id[rid]
                an, bn = active[a]["name"], active[b]["name"]
                paren = _wait_paren(r["min_wait_minutes"], r["max_wait_minutes"])
                da = dur[a]
                return r, an, bn, paren, da

            # 前向：沿“先于”方向陈述每条关系对开始时刻之差的下界（持续+最短等待）
            for idx, rid in enumerate(cyc_edges):
                a, b = cyc_nodes[idx], cyc_nodes[(idx + 1) % k]
                r, an, bn, paren, da = edge_parts(rid, a, b)
                lo_total = da + r["min_wait_minutes"]
                wait_part = "、最短等待 {} 分钟".format(r["min_wait_minutes"]) \
                    if r["min_wait_minutes"] else ""
                forward_chain.append({"text":
                    "「{}」先于「{}」{}：{}开始 − {}开始 ≥ {} 分钟（持续 {} 分钟{}）".format(
                        an, bn, paren, bn, an, lo_total, da, wait_part)})

            # 相反方向：后向回推。下界翻转为“最晚开始 − 常量”；存在最长等待时
            # 还给出开始时刻之差的上界（持续+最长等待），与前向各下界沿环相加矛盾。
            for idx in range(k - 1, -1, -1):
                rid = cyc_edges[idx]
                a, b = cyc_nodes[idx], cyc_nodes[(idx + 1) % k]
                r, an, bn, paren, da = edge_parts(rid, a, b)
                if r["max_wait_minutes"] is not None:
                    hi_total = da + r["max_wait_minutes"]
                    reverse_chain.append({"text":
                        "沿后向回推关系「{}」先于「{}」{}：{}开始 − {}开始 ≤ {} 分钟"
                        "（持续 {} 分钟、最长等待 {} 分钟）".format(
                            an, bn, paren, bn, an, hi_total, da, r["max_wait_minutes"])})
                else:
                    lo_total = da + r["min_wait_minutes"]
                    reverse_chain.append({"text":
                        "沿后向回推关系「{}」先于「{}」{}：「{}」最晚开始 ≤ 「{}」最晚开始 − {} 分钟".format(
                            an, bn, paren, an, bn, lo_total)})
        conflicts.append({
            "type": "cycle",
            "event_ids": cyc_nodes,
            "message": "“先于”关系构成循环：{}，间隔与持续要求沿环累加后约束不可满足".format(
                " → ".join(names + names[:1]) if names else "（未能定位具体环节）"),
            "forward_chain": forward_chain,
            "reverse_chain": reverse_chain,
        })

    # ---- 7. 窗口冲突（最早可行开始晚于最晚可行开始） ---------------------
    if not has_cycle:
        for i in active:
            if i in invalid_range:
                continue  # 已在输入层面报告
            if est[i] > lst[i]:
                conflicts.append({
                    "type": "window",
                    "event_ids": [i],
                    "message": "事件「{}」推算后最早可行开始 {} 晚于最晚可行开始 {}，约束冲突".format(
                        active[i]["name"], _fmt(est[i]), _fmt(lst[i])),
                    "earliest_chain": windows[i]["earliest_chain"],
                    "latest_chain": windows[i]["latest_chain"],
                })

    return {
        "status": "conflict" if conflicts else "ok",
        "windows": windows,
        "conflicts": conflicts,
        "drafts": drafts,
        "skipped_relations": skipped_relations,
    }
