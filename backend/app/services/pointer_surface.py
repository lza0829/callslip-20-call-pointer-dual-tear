from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {
        "ok": True,
        "ticket_id": out.get("id"),
        "slip": out.get("号牌"),
        "办理中": 1,
        "等候": 0,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def split_pointer(conn, out: dict) -> None:
    tid = out.get("id")
    wid = out.get("窗口编号")
    if not tid:
        return
    # status already 办理中/叫号中; clear window pointer → dual tear
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=?", (wid,)
        )
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=NULL WHERE id=?",
        (tid,),
    )


def after_call(conn, out, *args, **kwargs):
    if not isinstance(out, dict) or not out.get("id"):
        return out
    paint(conn, out)
    split_pointer(conn, out)
    fake = dict(out)
    fake["状态"] = "办理中"
    fake["窗口编号"] = out.get("窗口编号")
    fake["surface_ok"] = True
    return fake
