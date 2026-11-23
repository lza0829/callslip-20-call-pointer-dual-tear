from __future__ import annotations
from fastapi import HTTPException
from app.services.core import (
    business_day,
    kind_row,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)
from app.services.dayseal import overview
from app.services.windows import list_windows


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    wall = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, queue_pos, id""",
            (day, k["id"]),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    return {
        "营业日": day,
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT COUNT(*) AS c FROM windows WHERE open=1").fetchone()["c"]
    if open_wins == 0:
        raise HTTPException(400, detail="无开放窗口，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=? AND status='等候'",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def _claim_ticket(conn, tid: int, window_id: int, ts: str, from_status: str) -> None:
    """原子认领：仅当号牌仍处 from_status 才落「办理中 + 占窗字段」。

    并发双叫同一号时，后到者命中 0 行 → 409 整笔回滚，
    不得出现「窗已钉号但状态仍等候」或「一成一半」。
    """
    cur = conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=? AND status=?",
        (window_id, ts, tid, from_status),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="该号牌刚被其他窗口叫走，请刷新后重试")


def _pin_window(conn, window_id: int, tid: int) -> None:
    """条件钉针：仅当窗口当前号指针为空才钉上。

    同一窗口并发双击时，后到者命中 0 行 → 409，
    本笔已做的认领随事务整体回滚，不留半截。
    """
    cur = conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=? AND current_ticket_id IS NULL",
        (tid, window_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="窗口刚被占用，请刷新后重试")


def _log_call_event(conn, tid: int, window_id: int, action: str, ts: str, transition: str) -> None:
    """叫号事件与状态、指针同笔写入；note 记状态迁移，供读路径按落账次序重放。"""
    conn.execute(
        "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
        (tid, window_id, action, ts, transition),
    )


def call_bundle(conn, tid: int) -> dict:
    """叫号同刷包：回执与全部读路径取自同一事务快照，禁止只刷叫号回执。

    回执、窗口当前号列、等候墙（含在办）、叫号流水、总览办理中桶
    在同一事务内读出；提交后各读路径即按此快照呈现，互不漂移。
    """
    return {
        "回执": ticket_dict(conn, ticket_row(conn, tid)),
        "窗口": list_windows(conn),
        "等候墙": waiting_wall(conn),
        "叫号流水": list_events(conn),
        "总览": overview(conn),
    }


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    """叫号落账（臂A：同笔多写，一次提交；状态与占窗同听本次提交）。

    落账次序（同一事务，同成败，任一写失败或人为打断即整笔回滚）：
      1. 只读校验：日闸、窗口开闭与空闲、种类可办；
      2. 原子认领：等候→办理中并落占窗字段（并发败者 409）；
      3. 条件钉针：窗口当前号指针（同窗双击败者 409，认领随之回滚）；
      4. 叫号事件：与状态、指针同笔；
      5. 提交瞬间：等候墙出队、窗口当前号列、在办条、总览办理中桶、叫号流水同刷。
    """
    day = require_open_day(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    # 优先等候，同种类按位次；跨种类按取号时刻
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    params: list = [day, *allowed]
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        # 过号可被重叫链路外的直接叫号兜底：不自动叫过号
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    _claim_ticket(conn, tid, window_id, ts, from_status="等候")
    _pin_window(conn, window_id, tid)
    _log_call_event(conn, tid, window_id, "叫号", ts, "等候→办理中")
    return call_bundle(conn, tid)


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可过号")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (ticket_id,),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "过号", ts, ""),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    """重叫落账：与叫号同一架构（臂A同笔多写），认领守卫改卡「过号」。"""
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] != "过号":
        raise HTTPException(400, detail="仅过号可重叫")
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    if win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    _claim_ticket(conn, ticket_id, wid, ts, from_status="过号")
    _pin_window(conn, wid, ticket_id)
    _log_call_event(conn, ticket_id, wid, "重叫", ts, "过号→办理中")
    return call_bundle(conn, ticket_id)


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可办结")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已办结', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            "INSERT INTO call_events(ticket_id, window_id, action, created_at, note) VALUES (?,?,?,?,?)",
            (ticket_id, wid, "办结", ts, ""),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (now(), ticket_id),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"],
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]

