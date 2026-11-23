"""叫号探针：假钉窗收口的唯一全局口径（臂A · 同笔多写）。

以 窗口当前号 ∪ 号牌状态列 ∪ 叫号审计 同命运重放为准：
按落账次序重放窗指针事件（叫号/重叫/过号/办结）到当前提交点，
与已提交的号牌状态列、窗口钉针、各读路径逐一对照。
任何半截（一成一半）与双落（同号连点重复落账）都必须在此
对出对齐缺口；探针报「对齐」是成功灯成立的唯一判据。
"""
from __future__ import annotations

from app.services.core import business_day


def _window_names(conn) -> dict:
    return {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM windows").fetchall()
    }


def alignment_probe(conn) -> dict:
    """重放对照已提交状态，返回 {"对齐": bool, "缺口": [...]}。

    缺口种类（全局只此一套口径）：
      状态办中当前号空 / 当前号钉状态未办中 / 占窗字段与钉窗不一 /
      审计叫号无落账 / 落账无审计 / 审计动作无源 / 同号连叫双落 /
      钉窗与审计窗口不一 / 读路径漂移 / 序号页与发号流水不一
    """
    day = business_day(conn)
    gaps: list[dict] = []
    win_names = _window_names(conn)

    def gap(kind: str, detail: str, slip: str | None = None, wid: int | None = None):
        gaps.append(
            {
                "种类": kind,
                "号牌": slip,
                "窗口": win_names.get(wid) if wid else None,
                "细节": detail,
            }
        )

    tickets = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT * FROM tickets WHERE business_day=?", (day,)).fetchall()
    }
    slip_of = {tid: t["slip_no"] for tid, t in tickets.items()}

    # ── 钉针 ⟺ 状态列 ─────────────────────────────────────
    pins = {
        r["current_ticket_id"]: r["id"]
        for r in conn.execute(
            "SELECT id, current_ticket_id FROM windows WHERE current_ticket_id IS NOT NULL"
        ).fetchall()
    }
    busy = {tid: t for tid, t in tickets.items() if t["status"] == "办理中"}
    for tid in sorted(set(busy) - set(pins)):
        gap("状态办中当前号空", "号牌状态列已办中，但窗口当前号未钉该号", slip_of[tid],
            busy[tid]["window_id"])
    for tid in sorted(set(pins) - set(busy)):
        t = tickets.get(tid)
        gap(
            "当前号钉状态未办中",
            f"窗口当前号已钉，但号牌状态列为「{t['status'] if t else '不存在'}」",
            t["slip_no"] if t else None,
            pins[tid],
        )
    for tid in sorted(set(busy) & set(pins)):
        if busy[tid]["window_id"] != pins[tid]:
            gap(
                "占窗字段与钉窗不一",
                f"号牌占窗字段={busy[tid]['window_id']}，钉窗={pins[tid]}",
                slip_of[tid],
                pins[tid],
            )

    # ── 审计重放对照（窗指针事件 → 当前提交点）──────────────
    events = conn.execute(
        "SELECT * FROM call_events ORDER BY id"
    ).fetchall()
    open_call: dict[int, dict] = {}  # ticket_id → 最近一次未回收的叫号/重叫事件
    last_action: dict[int, dict] = {}
    for e in events:
        tid = e["ticket_id"]
        if e["action"] in ("叫号", "重叫"):
            if tid in open_call:
                gap(
                    "同号连叫双落",
                    f"事件#{e['id']} 重复叫号，上一笔事件#{open_call[tid]['id']} 尚未回收",
                    slip_of.get(tid),
                    e["window_id"],
                )
            open_call[tid] = dict(e)
        elif e["action"] in ("过号", "办结"):
            if tid not in open_call:
                gap(
                    "审计动作无源",
                    f"事件#{e['id']}「{e['action']}」没有未回收的叫号事件可对应",
                    slip_of.get(tid),
                    e["window_id"],
                )
            open_call.pop(tid, None)
        last_action[tid] = dict(e)
    for tid, e in open_call.items():
        t = tickets.get(tid)
        if not t or t["status"] != "办理中" or pins.get(tid) is None:
            gap(
                "审计叫号无落账",
                f"审计事件#{e['id']}「{e['action']}」成功，但状态列/当前号未落在办",
                slip_of.get(tid),
                e["window_id"],
            )
        elif pins.get(tid) != e["window_id"] or t["window_id"] != e["window_id"]:
            gap(
                "钉窗与审计窗口不一",
                f"审计记窗口={e['window_id']}，钉窗={pins.get(tid)}，占窗字段={t['window_id']}",
                slip_of.get(tid),
                e["window_id"],
            )
    for tid in sorted(set(busy) | set(pins)):
        if tid not in last_action or last_action[tid]["action"] not in ("叫号", "重叫"):
            gap(
                "落账无审计",
                "状态列/钉针宣称在办，但叫号审计无对应叫号事件，成功灯不可重放",
                slip_of.get(tid),
                pins.get(tid) or (busy.get(tid) or {}).get("window_id"),
            )

    # ── 读路径漂移（等候墙 / 总览桶）──────────────────────
    from app.services.dayseal import overview
    from app.services.tickets import waiting_wall

    wall = waiting_wall(conn)
    if {t["id"] for t in wall["在办"]} != set(pins):
        gap("读路径漂移", "等候墙在办条与窗口钉针集合不一致")
    queued = {t["id"] for b in wall["墙面"] for t in b["队列"]}
    expect_q = {tid for tid, t in tickets.items() if t["status"] in ("等候", "过号")}
    if queued != expect_q:
        gap("读路径漂移", "等候墙队列与等候+过号集合不一致")
    ov = overview(conn)
    if ov["办理中"] != len(busy):
        gap("读路径漂移", f"总览办理中桶={ov['办理中']}，状态列办理中={len(busy)}")
    if ov["等候"] != sum(1 for t in tickets.values() if t["status"] == "等候"):
        gap("读路径漂移", "总览等候桶与状态列等候数不一致")

    # ── 序号页 ⟂ 发号流水 ─────────────────────────────────
    kinds = conn.execute("SELECT id, prefix, seq FROM business_kinds").fetchall()
    for k in kinds:
        used = 0
        for t in tickets.values():
            if t["kind_id"] == k["id"] and t["slip_no"].startswith(k["prefix"]):
                try:
                    used = max(used, int(t["slip_no"][len(k["prefix"]):]))
                except ValueError:
                    continue
        if k["seq"] != used:
            gap(
                "序号页与发号流水不一",
                f"种类「{k['prefix']}」序号={k['seq']}，已发号到={used}",
            )

    return {
        "营业日": day,
        "对齐": not gaps,
        "缺口": gaps,
        "钉针数": len(pins),
        "办理中数": len(busy),
        "已核对事件数": len(events),
    }
