"""假钉窗夹具：四类撕裂态的探针对照与连点收敛（臂A 收口口径）。

夹具（注入撕裂态 → 叫号探针必须对出对齐缺口）：
  1. 状态办中当前号空；2. 当前号钉状态仍等候；
  3. 审计成功办理页无号；4. 同号连点半截双落。
口径：成功灯必须能由 当前号∪状态列∪叫号审计 同命运重放；
探针是假钉窗收口的唯一全局判据——旁路改状态字不算修完，
叫号 API、当前号接口、状态列、办理页、序号页、审计必须同刷。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import app.db as app_db
from app.db import db, init_db
from app.services import probe
from app.services import tickets as tic
from app.services.core import ticket_row
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
    """发号并同步种类序号，保持序号页与发号流水一致（撕裂只由夹具注入）。"""
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,'')""",
        (slip, kind_id, DAY, pos, f"{DAY}T09:00:00"),
    )
    prefix = conn.execute("SELECT prefix FROM business_kinds WHERE id=?", (kind_id,)).fetchone()["prefix"]
    conn.execute(
        "UPDATE business_kinds SET seq=MAX(seq, ?) WHERE id=?",
        (int(slip[len(prefix):]), kind_id),
    )
    return cur.lastrowid


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "callslip-probe.db")
    init_db()
    with db() as conn:
        seed_base(conn)
    return tmp_path / "callslip-probe.db"


def gap_kinds(result: dict) -> set:
    return {g["种类"] for g in result["缺口"]}


def test_fixture_busy_status_without_pin_is_flagged(fresh_db):
    """夹具一：状态列已办中、窗口当前号仍空——探针必须对出缺口。"""
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
        # 注入撕裂：只改状态列与占窗字段，不钉针、不记审计
        conn.execute(
            "UPDATE tickets SET status='办理中', window_id=1, called_at=? WHERE id=?",
            (f"{DAY}T09:30:00", tid),
        )
    with db() as conn:
        # 撕裂在 read path 上肉眼可见：状态列办中，当前号接口仍空
        t = ticket_row(conn, tid)
        assert t["status"] == "办理中"
        assert next(w for w in list_windows(conn) if w["id"] == 1)["当前号"] is None
        result = probe.alignment_probe(conn)
    assert not result["对齐"]
    assert gap_kinds(result) == {"状态办中当前号空", "落账无审计"}


def test_fixture_pin_without_busy_status_is_flagged(fresh_db):
    """夹具二：窗口当前号已钉、状态列仍等候——探针必须对出缺口。"""
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
        # 注入撕裂：只钉针，状态列不动、不记审计
        conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=1", (tid,))
    with db() as conn:
        t = ticket_row(conn, tid)
        assert t["status"] == "等候"
        assert next(w for w in list_windows(conn) if w["id"] == 1)["当前号"]["号牌"] == "储001"
        result = probe.alignment_probe(conn)
    assert not result["对齐"]
    assert gap_kinds(result) == {"当前号钉状态未办中", "落账无审计"}


def test_fixture_audit_success_without_landing_is_flagged(fresh_db):
    """夹具三：审计记叫号成功、办理页无号——探针重放必须对出缺口。"""
    with db() as conn:
        tid = add_ticket(conn, "储001", pos=1)
        # 注入撕裂：只写叫号审计，状态列与钉针都不动
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (tid, 1, "叫号", f"{DAY}T09:30:00", "等候→办理中"),
        )
    with db() as conn:
        # 办理页（等候墙在办条、窗口当前号）看不到该号，审计却宣称叫号成功
        assert tic.waiting_wall(conn)["在办"] == []
        assert next(w for w in list_windows(conn) if w["id"] == 1)["当前号"] is None
        assert tic.list_events(conn)[0]["动作"] == "叫号"
        result = probe.alignment_probe(conn)
    assert not result["对齐"]
    assert gap_kinds(result) == {"审计叫号无落账"}


def test_fixture_double_click_converges_without_double_landing(fresh_db):
    """夹具四：同号连点——状态列与当前号不得双落，探针对照窗指针事件必须干净。"""
    with db() as conn:
        add_ticket(conn, "储001", pos=1)
        add_ticket(conn, "储002", pos=2)

    def attempt(wid: int):
        try:
            with db() as conn:
                return ("ok", tic.call_next(conn, wid))
        except HTTPException as e:
            return ("http", e.status_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt, [1, 1]))  # 同一窗口连续点叫
    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "http"]
    assert len(oks) == 1 and len(fails) == 1, "连点必须恰一成一败，不得双落"
    assert fails[0][1] in (400, 409)
    with db() as conn:
        # 状态列与当前号各只落一笔：一个办理中、一根钉针、一条叫号事件
        assert conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='办理中'").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='等候'").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM call_events WHERE action='叫号'").fetchone()["c"] == 1
        result = probe.alignment_probe(conn)
        assert result["对齐"] and result["缺口"] == []
        # 探针与窗指针事件对照：钉针数=办理中数=已核对叫号事件数=1
        assert (result["钉针数"], result["办理中数"], result["已核对事件数"]) == (1, 1, 1)
        # 半截并发后再次点叫：干净判负，状态不动，探针仍对齐
        with pytest.raises(HTTPException) as ei:
            tic.call_next(conn, 1)
        assert ei.value.status_code == 400
    with db() as conn:
        assert probe.alignment_probe(conn)["对齐"]


def test_probe_stays_aligned_across_full_flow(fresh_db):
    """叫号链路每推进一步探针都必须对齐；回执不得与已提交状态拧花。"""
    with db() as conn:
        a = tic.take_ticket(conn, 1)["id"]  # 储001
        tic.take_ticket(conn, 1)            # 储002
        c = tic.take_ticket(conn, 2)["id"]  # 对001
    with db() as conn:
        bundle = tic.call_next(conn, 1)     # 储001 → 窗1
        assert probe.alignment_probe(conn)["对齐"]
    with db() as conn:
        # 回执与全新连接重读的已提交状态一致（禁止回执成功而状态拧花）
        fresh = ticket_row(conn, a)
        assert bundle["回执"]["状态"] == fresh["status"] == "办理中"
        assert bundle["回执"]["窗口编号"] == fresh["window_id"] == 1
    with db() as conn:
        tic.call_next(conn, 2)              # 对001 → 窗2
        assert probe.alignment_probe(conn)["对齐"]
    with db() as conn:
        tic.mark_skip(conn, a)              # 储001 过号，窗1 腾空
        assert probe.alignment_probe(conn)["对齐"]
    with db() as conn:
        tic.recall(conn, a, 1)              # 储001 重叫回窗1
        assert probe.alignment_probe(conn)["对齐"]
    with db() as conn:
        tic.finish(conn, c)                 # 对001 办结，窗2 腾空
        assert probe.alignment_probe(conn)["对齐"]
    with db() as conn:
        result = probe.alignment_probe(conn)
        assert result["对齐"] and result["缺口"] == []
        assert result["已核对事件数"] == conn.execute("SELECT COUNT(*) AS c FROM call_events").fetchone()["c"]


def test_call_api_and_read_endpoints_refresh_from_same_commit(fresh_db):
    """叫号 API、当前号接口、状态列、办理页、序号页、审计、总览必须同刷。"""
    from fastapi.testclient import TestClient

    import app.main as main

    with db() as conn:  # 清掉种子演示数据，只留夹具基线
        for table in ("call_events", "tickets", "windows", "business_kinds", "settings", "day_seals"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")  # 自增归零，夹具基线 id 可预期
        seed_base(conn)
    client = TestClient(main.app)

    assert client.post("/api/取号", json={"种类编号": 1}).json()["号牌"] == "储001"
    assert client.post("/api/取号", json={"种类编号": 1}).json()["号牌"] == "储002"
    r = client.post("/api/叫号", json={"窗口编号": 1})
    assert r.status_code == 200
    receipt = r.json()["回执"]
    assert receipt["号牌"] == "储001" and receipt["状态"] == "办理中"

    # 当前号接口：钉针与回执同号
    wins = client.get("/api/窗口").json()
    assert next(w for w in wins if w["id"] == 1)["当前号"]["号牌"] == "储001"
    # 状态列：办理中恰为回执号
    busy = client.get("/api/号牌", params={"状态": "办理中"}).json()
    assert [t["号牌"] for t in busy] == ["储001"]
    # 办理页（等候墙在办条 + 出队）
    wall = client.get("/api/等候墙").json()
    assert [t["号牌"] for t in wall["在办"]] == ["储001"]
    assert [t["号牌"] for b in wall["墙面"] for t in b["队列"]] == ["储002"]
    # 序号页：发了两号，序号记到 2，与发号流水一致
    kinds = client.get("/api/业务种类").json()
    assert next(k for k in kinds if k["前缀"] == "储")["序号"] == 2
    # 叫号审计：唯一一笔叫号，与回执同号同窗
    events = client.get("/api/叫号流水").json()
    assert len(events) == 1
    assert (events[0]["动作"], events[0]["号牌"], events[0]["备注"]) == ("叫号", "储001", "等候→办理中")
    # 总览办理中桶
    ov = client.get("/api/总览").json()
    assert (ov["办理中"], ov["等候"]) == (1, 1)
    assert "surface_ok" not in ov  # 假面字段不得再出现
    # 叫号探针：同一次提交重放对照，必须对齐
    result = client.get("/api/叫号探针").json()
    assert result["对齐"] and result["缺口"] == []
