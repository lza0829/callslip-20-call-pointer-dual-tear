"""叫号落账（臂A：同笔多写）正确性口径的可执行验证。

口径：号牌状态、窗口当前号指针、叫号事件、位次出队同听一次提交；
失败或人为打断整笔回到叫号前快照；并发双叫同一号恰一成一干净败；
读路径（回执/窗口当前号列/号牌状态列/等候墙/叫号流水/在办条/总览办理中桶）
必须能由落账次序重放，禁止只刷叫号回执。
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import app.db as app_db
from app.db import db, init_db
from app.services import tickets as tic
from app.services.core import ticket_row
from app.services.dayseal import overview
from app.services.windows import list_windows

DAY = "2026-11-23"


def seed_base(conn) -> None:
    conn.execute("INSERT INTO business_kinds(name, prefix, seq, active) VALUES ('个人储蓄','储',0,1)")
    conn.execute("INSERT INTO business_kinds(name, prefix, seq, active) VALUES ('对公结算','对',0,1)")
    conn.execute("INSERT INTO windows(name, kind_ids, open, current_ticket_id) VALUES ('一号窗','1,2',1,NULL)")
    conn.execute("INSERT INTO windows(name, kind_ids, open, current_ticket_id) VALUES ('二号窗','1,2',1,NULL)")
    conn.execute("INSERT INTO settings(key, value) VALUES ('营业日', ?)", (DAY,))
    conn.execute("INSERT INTO day_seals(business_day, sealed, sealed_at, note) VALUES (?,0,NULL,'')", (DAY,))


def add_ticket(conn, slip: str, kind_id: int = 1, pos: int = 1) -> int:
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,'')""",
        (slip, kind_id, DAY, pos, f"{DAY}T09:00:00"),
    )
    return cur.lastrowid


def snapshot(conn) -> dict:
    return {
        "tickets": [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id")],
        "windows": [dict(r) for r in conn.execute("SELECT * FROM windows ORDER BY id")],
        "events": [dict(r) for r in conn.execute("SELECT * FROM call_events ORDER BY id")],
    }


def assert_aligned(conn) -> None:
    """重放不变量：钉针⟺办理中；等候墙/总览/流水与已提交状态互洽，无可解释漂移。"""
    pins = {
        r["current_ticket_id"]: r["id"]
        for r in conn.execute("SELECT id, current_ticket_id FROM windows WHERE current_ticket_id IS NOT NULL")
    }
    busy = {r["id"]: r for r in conn.execute("SELECT * FROM tickets WHERE status='办理中'")}
    assert set(pins) == set(busy), "窗口钉针集合必须等于办理中集合"
    for tid, t in busy.items():
        assert t["window_id"] == pins[tid], "占窗字段必须与钉针窗口一致"
    wall = tic.waiting_wall(conn)
    assert {t["id"] for t in wall["在办"]} == set(pins), "等候墙在办必须等于钉针集合"
    queued = {t["id"] for b in wall["墙面"] for t in b["队列"]}
    expect_q = {r["id"] for r in conn.execute("SELECT id FROM tickets WHERE status IN ('等候','过号')")}
    assert queued == expect_q, "等候墙队列必须等于等候+过号集合"
    ov = overview(conn)
    assert ov["办理中"] == len(busy), "总览办理中桶必须等于办理中集合"
    assert ov["等候"] == conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='等候'").fetchone()["c"]
    for e in conn.execute("SELECT * FROM call_events WHERE action IN ('叫号','重叫')"):
        assert "→" in e["note"], "落账事件必须带状态迁移，供按落账次序重放"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "callslip-test.db")
    init_db()
    with db() as conn:
        seed_base(conn)
    return tmp_path / "callslip-test.db"


def attempt_call(wid: int):
    try:
        with db() as conn:
            return ("ok", tic.call_next(conn, wid))
    except HTTPException as e:
        return ("http", e.status_code)


def test_call_commit_refreshes_all_read_models(fresh_db):
    with db() as conn:
        add_ticket(conn, "储001", pos=1)
        add_ticket(conn, "储002", pos=2)
    with db() as conn:
        bundle = tic.call_next(conn, 1)
        assert_aligned(conn)
    # 叫号回执
    assert bundle["回执"]["号牌"] == "储001"
    assert bundle["回执"]["状态"] == "办理中"
    assert bundle["回执"]["窗口编号"] == 1
    # 窗口当前号列（同刷包内）
    w1 = next(w for w in bundle["窗口"] if w["id"] == 1)
    w2 = next(w for w in bundle["窗口"] if w["id"] == 2)
    assert w1["当前号"]["号牌"] == "储001"
    assert w2["当前号"] is None
    # 等候墙：位次出队 + 在办条，不得新增可解释漂移
    wall = bundle["等候墙"]
    assert {t["号牌"] for b in wall["墙面"] for t in b["队列"]} == {"储002"}
    assert {t["号牌"] for t in wall["在办"]} == {"储001"}
    assert next(b for b in wall["墙面"] if b["前缀"] == "储")["等候数"] == 1
    # 叫号事件流水
    ev = bundle["叫号流水"][0]
    assert (ev["动作"], ev["号牌"], ev["备注"]) == ("叫号", "储001", "等候→办理中")
    # 总览办理中桶
    assert bundle["总览"]["办理中"] == 1
    assert bundle["总览"]["等候"] == 1
    # 提交后全新连接重读：与同刷包一致（读路径 = 落账次序重放到当前提交点）
    with db() as conn:
        assert {t["号牌"] for t in tic.waiting_wall(conn)["在办"]} == {"储001"}
        ov = overview(conn)
        assert (ov["办理中"], ov["等候"]) == (1, 1)
        assert next(w for w in list_windows(conn) if w["id"] == 1)["当前号"]["号牌"] == "储001"


def test_mid_write_failure_rolls_back_to_pre_call_snapshot(fresh_db):
    with db() as conn:
        add_ticket(conn, "储001", pos=1)
        before = snapshot(conn)
        conn.execute("DROP TABLE call_events")  # 令多写中的事件写必败
    with pytest.raises(sqlite3.OperationalError):
        with db() as conn:
            tic.call_next(conn, 1)
    init_db()  # 重建事件表
    with db() as conn:
        assert snapshot(conn) == before, "任一侧失败，状态/指针/事件须整笔回到叫号前快照"


def test_manual_interruption_rolls_back_everything(fresh_db):
    with db() as conn:
        add_ticket(conn, "储001", pos=1)
        before = snapshot(conn)
    with pytest.raises(RuntimeError):
        with db() as conn:
            tic.call_next(conn, 1)  # 多写全部落笔
            raise RuntimeError("人为打断：提交前中断")
    with db() as conn:
        assert snapshot(conn) == before, "人为打断多写须全回滚"


def test_concurrent_double_call_same_ticket_exactly_one_wins(fresh_db):
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt_call, [1, 2]))
    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "http"]
    assert len(oks) == 1 and len(fails) == 1, "并发双叫同一号：恰一成一败，不得一成一半"
    assert fails[0][1] in (400, 409), "败者须干净判负（已无可叫/刚被叫走），不得 500 假绿"
    with db() as conn:
        t = ticket_row(conn, tid)
        assert t["status"] == "办理中"
        winner = t["window_id"]
        pins = conn.execute(
            "SELECT id, current_ticket_id FROM windows WHERE current_ticket_id IS NOT NULL"
        ).fetchall()
        assert len(pins) == 1 and pins[0]["id"] == winner and pins[0]["current_ticket_id"] == tid
        evs = conn.execute("SELECT * FROM call_events WHERE action='叫号'").fetchall()
        assert len(evs) == 1 and evs[0]["ticket_id"] == tid and evs[0]["window_id"] == winner
        assert_aligned(conn)


def test_concurrent_same_window_double_click(fresh_db):
    with db() as conn:
        add_ticket(conn, "储001", pos=1)
        add_ticket(conn, "储002", pos=2)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt_call, [1, 1]))
    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "http"]
    assert len(oks) == 1 and len(fails) == 1
    assert fails[0][1] in (400, 409)
    with db() as conn:
        busy = conn.execute("SELECT * FROM tickets WHERE status='办理中'").fetchall()
        waiting = conn.execute("SELECT * FROM tickets WHERE status='等候'").fetchall()
        assert len(busy) == 1 and len(waiting) == 1, "败者认领须随事务回滚，号牌回到等候"
        assert busy[0]["window_id"] == 1
        win = conn.execute("SELECT current_ticket_id FROM windows WHERE id=1").fetchone()
        assert win["current_ticket_id"] == busy[0]["id"]
        assert len(conn.execute("SELECT * FROM call_events WHERE action='叫号'").fetchall()) == 1
        assert_aligned(conn)


def test_read_models_replay_consistency_across_flow(fresh_db):
    with db() as conn:
        a = tic.take_ticket(conn, 1)["id"]  # 储001
        b = tic.take_ticket(conn, 1)["id"]  # 储002
        c = tic.take_ticket(conn, 2)["id"]  # 对001
    with db() as conn:
        tic.call_next(conn, 1)  # 储001 → 窗1
        assert_aligned(conn)
    with db() as conn:
        tic.call_next(conn, 2)  # 对001（位次1）→ 窗2
        assert_aligned(conn)
    with db() as conn:
        tic.mark_skip(conn, a)  # 储001 过号，窗1 腾空
        assert_aligned(conn)
    with db() as conn:
        tic.recall(conn, a, 1)  # 储001 重叫回窗1
        assert_aligned(conn)
    with db() as conn:
        tic.finish(conn, c)  # 对001 办结，窗2 腾空
        assert_aligned(conn)
    with db() as conn:
        acts = [r["action"] for r in conn.execute("SELECT action FROM call_events ORDER BY id")]
        assert acts == ["叫号", "叫号", "过号", "重叫", "办结"]
        trans = [
            r["note"]
            for r in conn.execute(
                "SELECT note FROM call_events WHERE action IN ('叫号','重叫') ORDER BY id"
            )
        ]
        assert trans == ["等候→办理中", "等候→办理中", "过号→办理中"]
        # 终态：储001 在办于窗1，储002 仍等候，对001 已办结
        assert ticket_row(conn, a)["status"] == "办理中"
        assert ticket_row(conn, b)["status"] == "等候"
        assert ticket_row(conn, c)["status"] == "已办结"


def test_recall_atomic_and_guarded(fresh_db):
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
    with db() as conn:
        tic.call_next(conn, 1)
    with db() as conn:
        tic.mark_skip(conn, tid)
    with db() as conn:
        bundle = tic.recall(conn, tid, 2)
        assert bundle["回执"]["状态"] == "办理中"
        assert bundle["回执"]["窗口编号"] == 2
        ev = bundle["叫号流水"][0]
        assert (ev["动作"], ev["备注"]) == ("重叫", "过号→办理中")
        assert (bundle["总览"]["过号"], bundle["总览"]["办理中"]) == (0, 1)
        assert_aligned(conn)
    with db() as conn:
        with pytest.raises(HTTPException) as ei:
            tic.recall(conn, tid, 2)  # 已在办，不可再重叫
        assert ei.value.status_code == 400


def test_concurrent_double_recall_exactly_one_wins(fresh_db):
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
    with db() as conn:
        tic.call_next(conn, 1)
    with db() as conn:
        tic.mark_skip(conn, tid)

    def attempt_recall(wid: int):
        try:
            with db() as conn:
                return ("ok", tic.recall(conn, tid, wid))
        except HTTPException as e:
            return ("http", e.status_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt_recall, [1, 2]))
    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "http"]
    assert len(oks) == 1 and len(fails) == 1, "并发双重叫同一号：恰一成一败"
    assert fails[0][1] in (400, 409)
    with db() as conn:
        t = ticket_row(conn, tid)
        assert t["status"] == "办理中"
        pins = conn.execute(
            "SELECT id, current_ticket_id FROM windows WHERE current_ticket_id IS NOT NULL"
        ).fetchall()
        assert len(pins) == 1 and pins[0]["current_ticket_id"] == tid
        assert len(conn.execute("SELECT * FROM call_events WHERE action='重叫'").fetchall()) == 1
        assert_aligned(conn)
