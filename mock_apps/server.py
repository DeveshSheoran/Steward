"""Mock "Northwind Retail" company systems: Admin Console, Helpdesk, Billing.

Plain server-rendered HTML forms (like real internal tools) in ONE process, so the demo needs no setup.
Not part of the Steward core: swapping these for real systems means editing the Company Pack only.

Chaos controls (POST /__chaos, JSON):
  fail_next_mutations: N   -> next N write requests return 503 *before* applying anything
  fail_after_apply: N      -> next N writes are APPLIED but respond 503 (tests idempotency of retries)
  slow_ms: ms              -> artificial latency on every request
  ui_variant: "A" | "B"    -> variant B renames buttons/labels (tests that nothing is hard-coded)
Ground truth for evals: GET /__state ; reset: POST /__reset
"""
from __future__ import annotations

import asyncio
import copy
import html
import threading
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SEED = {
    "orders": {
        "A-1001": {"id": "A-1001", "customer": "Rohan Mehta", "email": "rohan@example.com", "plan": "standard",
                   "amount": 1500, "refunded": 0, "status": "paid", "note": "Charged twice for Oct plan"},
        "A-1002": {"id": "A-1002", "customer": "Ananya Rao", "email": "ananya@example.com", "plan": "standard",
                   "amount": 4200, "refunded": 0, "status": "paid", "note": "Damaged item received"},
        "A-1003": {"id": "A-1003", "customer": "Acme Logistics", "email": "ops@acme-logistics.example", "plan": "enterprise",
                   "amount": 18000, "refunded": 0, "status": "paid", "note": "Enterprise annual plan"},
    },
    "refunds": [],
    "tickets": {
        "T-501": {"id": "T-501", "subject": "Charged twice for October", "customer": "Rohan Mehta", "email": "rohan@example.com",
                  "order_id": "A-1001", "status": "open", "body": "I was charged twice for my October plan. Please refund the duplicate charge.", "replies": []},
        "T-502": {"id": "T-502", "subject": "Item arrived damaged", "customer": "Ananya Rao", "email": "ananya@example.com",
                  "order_id": "A-1002", "status": "open", "body": "My order arrived damaged. I would like a full refund.", "replies": []},
        "T-503": {"id": "T-503", "subject": "Refund for enterprise plan", "customer": "Acme Logistics", "email": "ops@acme-logistics.example",
                  "order_id": "A-1003", "status": "open", "body": "We are cancelling early. Please refund the annual plan.", "replies": []},
    },
    "users": {
        "priya@northwind.example": {"email": "priya@northwind.example", "name": "Priya Nair", "department": "Finance",
                                    "title": "Finance Manager", "groups": ["staff", "finance"], "active": True},
        "dev@northwind.example": {"email": "dev@northwind.example", "name": "Dev Patel", "department": "Engineering",
                                  "title": "Engineering Manager", "groups": ["staff", "engineering"], "active": True},
    },
    "groups": ["staff", "engineering", "finance", "sales", "admins", "finance-admins"],
    "requests": [],
}

_lock = threading.Lock()
STATE = copy.deepcopy(SEED)
CHAOS = {"fail_next_mutations": 0, "fail_after_apply": 0, "slow_ms": 0, "ui_variant": "A"}
IDEMP: dict[str, str] = {}      # Idempotency-Key -> redirect location of the first successful apply
HITS = {"mutations": 0}

app = FastAPI(title="Northwind mock systems")


def esc(x) -> str:
    return html.escape(str(x))


def page(title: str, body: str, nav: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>body{{font:15px system-ui;margin:0}}header{{background:#1f2937;color:#fff;padding:10px 20px}}
header a{{color:#cbd5e1;margin-right:16px;text-decoration:none}}main{{padding:20px;max-width:880px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #d1d5db;padding:6px 10px;text-align:left}}
label{{display:block;margin:10px 0 2px}}input,select,textarea{{padding:6px;width:320px}}button{{margin-top:12px;padding:8px 14px}}
.flash{{background:#ecfdf5;border:1px solid #10b981;padding:8px;margin-bottom:12px}}.err{{background:#fef2f2;border:1px solid #ef4444;padding:8px}}</style>
</head><body><header><b>Northwind</b> &nbsp; <a href="/admin/users">Admin Console</a><a href="/helpdesk/tickets">Helpdesk</a><a href="/billing/orders">Billing</a></header>
<main><h2>{esc(title)}</h2>{body}</main></body></html>""")


def flash(request: Request) -> str:
    m = request.query_params.get("msg")
    return f'<div class="flash" role="status">{esc(m)}</div>' if m else ""


async def form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode()
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


@app.middleware("http")
async def chaos_mw(request: Request, call_next):
    if CHAOS["slow_ms"]:
        await asyncio.sleep(CHAOS["slow_ms"] / 1000)
    if request.method == "POST" and not request.url.path.startswith("/__"):
        key = request.headers.get("idempotency-key")
        with _lock:
            HITS["mutations"] += 1
            if key and key in IDEMP:                                   # replay of an already-applied write
                return RedirectResponse(IDEMP[key], status_code=303)
            if CHAOS["fail_next_mutations"] > 0:
                CHAOS["fail_next_mutations"] -= 1
                return HTMLResponse("<h1>503 Service Unavailable</h1>", status_code=503)
        resp = await call_next(request)
        with _lock:
            if key and resp.status_code in (302, 303):
                IDEMP[key] = resp.headers.get("location", "/")
            if CHAOS["fail_after_apply"] > 0 and resp.status_code in (302, 303):
                CHAOS["fail_after_apply"] -= 1
                return HTMLResponse("<h1>503 Service Unavailable (after apply)</h1>", status_code=503)
        return resp
    return await call_next(request)


# ------------------------------------------------------------------ control plane
@app.get("/__state")
def state():
    with _lock:
        return JSONResponse({**STATE, "_chaos": CHAOS, "_hits": HITS})


@app.post("/__reset")
def reset():
    with _lock:
        STATE.clear(); STATE.update(copy.deepcopy(SEED))
        CHAOS.update({"fail_next_mutations": 0, "fail_after_apply": 0, "slow_ms": 0, "ui_variant": "A"})
        IDEMP.clear(); HITS["mutations"] = 0
    return {"ok": True}


@app.post("/__chaos")
async def chaos(request: Request):
    CHAOS.update(await request.json())
    return CHAOS


@app.get("/")
def root():
    return RedirectResponse("/helpdesk/tickets")


# ------------------------------------------------------------------ billing
@app.get("/billing/orders")
def orders(request: Request):
    rows = "".join(f'<tr><td><a href="/billing/orders/{o["id"]}">{o["id"]}</a></td><td>{esc(o["customer"])}</td>'
                   f'<td>{esc(o["plan"])}</td><td>{o["amount"]}</td><td>{o["refunded"]}</td><td>{esc(o["status"])}</td></tr>'
                   for o in STATE["orders"].values())
    return page("Billing: orders", flash(request) + f"<table><tr><th>Order</th><th>Customer</th><th>Plan</th><th>Amount (INR)</th><th>Refunded</th><th>Status</th></tr>{rows}</table>")


@app.get("/billing/orders/{oid}")
def order(oid: str, request: Request):
    o = STATE["orders"].get(oid)
    if not o:
        return page("Not found", '<div class="err">No such order</div>')
    v = CHAOS["ui_variant"]
    btn, lab = ("Issue refund", "Refund amount (INR)") if v == "A" else ("Process credit note", "Credit amount (INR)")
    refs = "".join(f"<li>{r['id']}: INR {r['amount']} ({esc(r['reason'])})</li>" for r in STATE["refunds"] if r["order_id"] == oid)
    return page(f"Order {oid}", flash(request) + f"""<p>Customer: {esc(o['customer'])} ({esc(o['email'])})<br>Plan: {esc(o['plan'])}<br>
Amount paid: INR {o['amount']}<br>Already refunded: INR {o['refunded']}<br>Status: {esc(o['status'])}<br>Note: {esc(o['note'])}</p>
<h3>Refund history</h3><ul>{refs or '<li>none</li>'}</ul>
<form method="post" action="/billing/refunds"><input type="hidden" name="order_id" value="{oid}">
<label for="amount">{lab}</label><input id="amount" name="amount" type="number" min="1">
<label for="reason">Reason</label><input id="reason" name="reason" type="text">
<button type="submit">{btn}</button></form>""")


@app.post("/billing/refunds")
async def create_refund(request: Request):
    f = await form(request)
    with _lock:
        o = STATE["orders"].get(f.get("order_id", ""))
        try:
            amt = int(float(f.get("amount", "0")))
        except ValueError:
            amt = 0
        if not o or amt <= 0 or amt > o["amount"] - o["refunded"]:
            return page("Refund failed", '<div class="err">Invalid order or amount exceeds refundable balance</div>')
        rid = f"R-{len(STATE['refunds']) + 1:04d}"
        STATE["refunds"].append({"id": rid, "order_id": o["id"], "amount": amt, "reason": f.get("reason", "")})
        o["refunded"] += amt
        if o["refunded"] == o["amount"]:
            o["status"] = "refunded"
    return RedirectResponse(f"/billing/orders/{o['id']}?msg=Refund+{rid}+recorded", status_code=303)


@app.get("/billing/api/orders")
def api_orders():
    return list(STATE["orders"].values())


@app.get("/billing/api/orders/{oid}")
def api_order(oid: str):
    o = STATE["orders"].get(oid)
    return JSONResponse(o if o else {"error": "not found"}, status_code=200 if o else 404)


# ------------------------------------------------------------------ helpdesk
@app.get("/helpdesk/tickets")
def tickets(request: Request):
    rows = "".join(f'<tr><td><a href="/helpdesk/tickets/{t["id"]}">{t["id"]}</a></td><td>{esc(t["subject"])}</td>'
                   f'<td>{esc(t["customer"])}</td><td>{esc(t["status"])}</td></tr>' for t in STATE["tickets"].values())
    return page("Helpdesk: tickets", flash(request) + f"<table><tr><th>Ticket</th><th>Subject</th><th>Customer</th><th>Status</th></tr>{rows}</table>")


@app.get("/helpdesk/tickets/{tid}")
def ticket(tid: str, request: Request):
    t = STATE["tickets"].get(tid)
    if not t:
        return page("Not found", '<div class="err">No such ticket</div>')
    reps = "".join(f"<li><b>{esc(r['author'])}</b>: {esc(r['body'])}</li>" for r in t["replies"])
    return page(f"Ticket {tid}", flash(request) + f"""<p>From: {esc(t['customer'])} ({esc(t['email'])})<br>Order: {esc(t['order_id'])}<br>
Status: {esc(t['status'])}<br>Subject: {esc(t['subject'])}</p><blockquote>{esc(t['body'])}</blockquote>
<h3>Replies</h3><ul>{reps or '<li>none</li>'}</ul>
<form method="post" action="/helpdesk/tickets/{tid}/reply"><label for="body">Reply to customer</label>
<textarea id="body" name="body" rows="4"></textarea><label for="status">Set status</label>
<select id="status" name="status"><option value="open">open</option><option value="pending">pending</option><option value="resolved">resolved</option></select>
<button type="submit">Send reply</button></form>""")


@app.post("/helpdesk/tickets/{tid}/reply")
async def reply(tid: str, request: Request):
    f = await form(request)
    with _lock:
        t = STATE["tickets"].get(tid)
        if not t or not f.get("body", "").strip():
            return page("Reply failed", '<div class="err">Empty reply or unknown ticket</div>')
        t["replies"].append({"author": "support", "body": f["body"]})
        t["status"] = f.get("status", t["status"])
    return RedirectResponse(f"/helpdesk/tickets/{tid}?msg=Reply+sent", status_code=303)


# ------------------------------------------------------------------ admin console
@app.get("/admin/users")
def users(request: Request):
    rows = "".join(f'<tr><td><a href="/admin/users/{u["email"]}">{esc(u["email"])}</a></td><td>{esc(u["name"])}</td>'
                   f'<td>{esc(u["department"])}</td><td>{esc(", ".join(u["groups"]))}</td></tr>' for u in STATE["users"].values())
    v = CHAOS["ui_variant"]
    return page("Admin: users", flash(request) + f"""<table><tr><th>Email</th><th>Name</th><th>Department</th><th>Groups</th></tr>{rows}</table>
<h3>{'Create user' if v == 'A' else 'Provision account'}</h3>
<form method="post" action="/admin/users"><label for="name">Full name</label><input id="name" name="name">
<label for="email">Email</label><input id="email" name="email"><label for="department">Department</label><input id="department" name="department">
<label for="title">Job title</label><input id="title" name="title"><button type="submit">{'Create user' if v == 'A' else 'Provision'}</button></form>
<p><a href="/admin/requests">Service requests</a></p>""")


@app.post("/admin/users")
async def create_user(request: Request):
    f = await form(request)
    email = f.get("email", "").strip().lower()
    with _lock:
        if not email.endswith("@northwind.example") or email in STATE["users"] or not f.get("name", "").strip():
            return page("Create failed", '<div class="err">Invalid email/name or user already exists</div>')
        STATE["users"][email] = {"email": email, "name": f["name"], "department": f.get("department", ""),
                                 "title": f.get("title", ""), "groups": ["staff"], "active": True}
    return RedirectResponse(f"/admin/users/{email}?msg=User+created", status_code=303)


@app.get("/admin/users/{email}")
def user(email: str, request: Request):
    u = STATE["users"].get(email)
    if not u:
        return page("Not found", '<div class="err">No such user</div>')
    opts = "".join(f"<option>{g}</option>" for g in STATE["groups"] if g not in u["groups"])
    return page(f"User {u['name']}", flash(request) + f"""<p>Email: {esc(u['email'])}<br>Department: {esc(u['department'])}<br>
Title: {esc(u['title'])}<br>Groups: {esc(', '.join(u['groups']))}</p>
<form method="post" action="/admin/users/{email}/groups"><label for="group">Add to group</label>
<select id="group" name="group">{opts}</select><button type="submit">Add to group</button></form>
<form method="post" action="/admin/requests"><input type="hidden" name="user_email" value="{esc(email)}">
<input type="hidden" name="kind" value="laptop"><label for="model">Laptop model</label>
<select id="model" name="model"><option>Standard 14</option><option>Developer 16</option></select>
<button type="submit">Request laptop</button></form>""")


@app.post("/admin/users/{email}/groups")
async def add_group(email: str, request: Request):
    f = await form(request)
    with _lock:
        u = STATE["users"].get(email)
        if not u or f.get("group") not in STATE["groups"]:
            return page("Failed", '<div class="err">Unknown user or group</div>')
        if f["group"] not in u["groups"]:
            u["groups"].append(f["group"])
    return RedirectResponse(f"/admin/users/{email}?msg=Added+to+{f['group']}", status_code=303)


@app.get("/admin/requests")
def requests_page(request: Request):
    rows = "".join(f"<tr><td>{r['id']}</td><td>{esc(r['kind'])}</td><td>{esc(r['user_email'])}</td><td>{esc(r.get('model',''))}</td></tr>"
                   for r in STATE["requests"])
    return page("Admin: service requests", flash(request) + f"<table><tr><th>ID</th><th>Kind</th><th>User</th><th>Model</th></tr>{rows}</table>")


@app.post("/admin/requests")
async def create_request(request: Request):
    f = await form(request)
    with _lock:
        if f.get("user_email") not in STATE["users"]:
            return page("Failed", '<div class="err">Unknown user</div>')
        rid = f"REQ-{len(STATE['requests']) + 1:03d}"
        STATE["requests"].append({"id": rid, "kind": f.get("kind", "laptop"), "user_email": f["user_email"], "model": f.get("model", "")})
    return RedirectResponse(f"/admin/requests?msg={rid}+created", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="warning")
