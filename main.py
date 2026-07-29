# -*- coding: utf-8 -*-
"""
Portal B2B — backend FastAPI
Proxy entre el HTML del portal y la API JSON-RPC de Odoo 17.
Cada usuario se autentica con sus propias credenciales de Odoo.
No hay cuenta de servicio hardcodeada.
"""

import os
import secrets
import httpx
from contextlib import asynccontextmanager
from typing import Any, Annotated

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# Config (solo URL y BD — sin credenciales)
# ---------------------------------------------------------------------------
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8071")
ODOO_DB = os.getenv("ODOO_DB", "myuniform")

# ---------------------------------------------------------------------------
# Sesiones de portal: token (cookie httpOnly) → datos de sesión Odoo
# {portal_token: {uid, odoo_session_id, name, partner_id}}
# En producción real esto iría a Redis; aquí memoria en proceso es suficiente.
# ---------------------------------------------------------------------------
_sessions: dict[str, dict] = {}
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=30.0)
    yield
    await _client.aclose()


app = FastAPI(title="Portal B2B — MyUniform", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers JSON-RPC
# ---------------------------------------------------------------------------


async def _rpc(endpoint: str, params: dict, odoo_session_id: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if odoo_session_id:
        headers["Cookie"] = f"session_id={odoo_session_id}"
    body = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
    r = await _client.post(f"{ODOO_URL}{endpoint}", json=body, headers=headers)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        detail = data["error"].get("data", {}).get("message") or data["error"].get(
            "message", "Error Odoo"
        )
        import sys

        print(f"ODOO_ERR [{endpoint}]: {detail!r}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=400, detail=detail)
    result = data.get("result")
    # Odoo 17 NO incluye session_id en el cuerpo JSON de
    # /web/session/authenticate, solo como cookie Set-Cookie. Sin esto,
    # login() guardaba odoo_session_id=None y todo dependía por accidente
    # del cookie-jar compartido de _client (que mezcla sesiones entre
    # usuarios distintos, justo lo que la arquitectura dice evitar).
    sid = r.cookies.get("session_id")
    if sid and isinstance(result, dict) and "session_id" not in result:
        result["session_id"] = sid
    return result


async def _call_kw(
    session: dict, model: str, method: str, args: list, kwargs: dict | None = None
) -> Any:
    return await _rpc(
        "/web/dataset/call_kw",
        {
            "model": model,
            "method": method,
            "args": args,
            "kwargs": kwargs or {"context": {"lang": "es_ES"}},
        },
        odoo_session_id=session["odoo_session_id"],
    )


# ---------------------------------------------------------------------------
# Dependencia de autenticación
# ---------------------------------------------------------------------------


async def get_session(portal_token: Annotated[str | None, Cookie()] = None) -> dict:
    if not portal_token or portal_token not in _sessions:
        raise HTTPException(status_code=401, detail="Sesión no iniciada o expirada")
    return _sessions[portal_token]


SessionDep = Annotated[dict, Depends(get_session)]


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    try:
        result = await _rpc(
            "/web/session/authenticate",
            {
                "db": ODOO_DB,
                "login": body.get("login", ""),
                "password": body.get("password", ""),
            },
        )
    except HTTPException:
        # _rpc() ya tradujo un error JSON-RPC de Odoo (p.ej. AccessDenied por
        # credenciales incorrectas) a una HTTPException con el detalle real.
        # Odoo 17 responde así a credenciales incorrectas (no con uid=False),
        # así que aquí se traduce a 401 en vez de ocultarlo como error de conexión.
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    except Exception as e:
        if "Name or service not known" in str(e) or "ConnectError" in str(
            type(e).__name__
        ):
            raise HTTPException(
                status_code=503,
                detail="Servidor MyUniform no disponible. Por favor, intenta más tarde.",
            )
        raise HTTPException(
            status_code=500, detail="Error de conexión con el servidor."
        )

    if not result or not result.get("uid"):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")

    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "uid": result["uid"],
        "odoo_session_id": result.get("session_id"),
        "name": result.get("name"),
        "partner_id": result.get("partner_id"),
        "login": body.get("login"),
    }
    response.set_cookie(
        "portal_token",
        token,
        httponly=True,
        samesite="lax",
        secure=False,  # secure=True en HTTPS
        max_age=28800,  # 8 horas
    )
    return {
        "uid": result["uid"],
        "name": result.get("name"),
        "partner_id": result.get("partner_id"),
    }


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("portal_token")
    if token:
        _sessions.pop(token, None)
    response.delete_cookie("portal_token")
    return {"ok": True}


@app.get("/api/me")
async def me(session: SessionDep):
    login_date = None
    try:
        users = await _call_kw(
            session,
            "res.users",
            "read",
            [[session["uid"]]],
            {"fields": ["login_date"]},
        )
        if users:
            login_date = users[0].get("login_date")
    except Exception:
        pass
    return {
        "uid": session["uid"],
        "name": session["name"],
        "partner_id": session["partner_id"],
        "login": session["login"],
        "login_date": login_date,
    }


@app.post("/api/me/password")
async def change_my_password(request: Request, session: SessionDep):
    body = await request.json()
    old = (body.get("old_password") or "").strip()
    new = (body.get("new_password") or "").strip()
    if not old or not new:
        raise HTTPException(400, "Debes indicar la contraseña actual y la nueva")
    if len(new) < 8:
        raise HTTPException(400, "La nueva contraseña debe tener al menos 8 caracteres")
    await _call_kw(session, "res.users", "change_password", [old, new])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Portal — mensajes de bienvenida
# ---------------------------------------------------------------------------


@app.get("/api/portal_messages")
async def list_portal_messages(session: SessionDep, limit: int = 20):
    """Mensajes de la gestora de cuenta para el panel de bienvenida (v-dash).
    El ir.rule del modelo ya filtra a mensajes generales + los del propio cliente."""
    return await _call_kw(
        session,
        "uniform.portal.message",
        "search_read",
        [[]],
        {
            "fields": ["id", "body", "date", "partner_id"],
            "limit": limit,
            "order": "date desc",
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/portal_promos")
async def list_portal_promos(session: SessionDep, limit: int = 20):
    """Promociones/novedades de MY Uniform para el panel Portal.
    El ir.rule del modelo ya filtra a promos generales + las del propio cliente."""
    return await _call_kw(
        session,
        "uniform.portal.promo",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "title",
                "subtitle",
                "image_128",
                "color_from",
                "color_to",
                "cta_label",
                "cta_url",
                "date",
            ],
            "limit": limit,
            "order": "sequence, date desc",
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/portal/pending_sizes")
async def portal_pending_sizes(session: SessionDep, limit: int = 5):
    """Tallas asignadas a trabajadores en pedidos confirmados que aún no se han
    entregado del todo (product_uom_qty > qty_delivered). Datos reales de
    sale.order.line, restringidos por el ir.rule estándar de portal en el
    propio sale.order (partner_id child_of)."""
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [
            [
                ["worker_ids", "!=", False],
                ["display_type", "=", False],
                ["order_id.state", "in", ["sale", "done"]],
            ]
        ],
        {
            "fields": [
                "id",
                "product_id",
                "product_uom_qty",
                "qty_delivered",
                "product_size_value",
                "worker_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    pending_lines = [
        l for l in lines if (l["product_uom_qty"] - l["qty_delivered"]) > 0.001
    ]
    worker_ids = list(
        {wid for l in pending_lines for wid in (l.get("worker_ids") or [])}
    )
    worker_map: dict = {}
    if worker_ids:
        workers = await _call_kw(
            session,
            "uniform.agreement.worker",
            "read",
            [worker_ids],
            {
                "fields": ["id", "name", "code", "delegation_id"],
                "context": {"lang": "es_ES"},
            },
        )
        worker_map = {w["id"]: w for w in workers}
    rows = []
    for l in pending_lines:
        pending_qty = l["product_uom_qty"] - l["qty_delivered"]
        for wid in l.get("worker_ids") or []:
            w = worker_map.get(wid)
            if not w:
                continue
            rows.append(
                {
                    "line_id": l["id"],
                    "worker_id": wid,
                    "worker_name": w.get("name") or "",
                    "worker_code": w.get("code") or "",
                    "delegation_name": (w.get("delegation_id") or [None, ""])[1],
                    "product_name": (l.get("product_id") or [None, ""])[1],
                    "size": l.get("product_size_value") or "",
                    "pending_qty": pending_qty,
                }
            )
    rows.sort(key=lambda r: -r["pending_qty"])
    total_qty = sum(r["pending_qty"] for r in rows)
    return {"rows": rows[:limit], "total": len(rows), "total_qty": total_qty}


@app.get("/api/portal/stock_alerts")
async def portal_stock_alerts(
    session: SessionDep, limit: int = 5, threshold: float = 0.10
):
    """Prendas con menos de <threshold> del cupo del acuerdo disponible
    (remaining_quantity / (remaining_quantity + used_quantity)). used_quantity
    y remaining_quantity ya los calcula edyma_myuniform (sale_order_line.py)
    comparando pedidos relacionados del mismo acuerdo — es el cupo real, no
    stock físico de almacén."""
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [
            [
                ["display_type", "=", False],
                ["order_id.state", "in", ["sale", "done"]],
                ["order_id.uniform_agreement_id", "!=", False],
            ]
        ],
        {"fields": _LINE_FIELDS, "context": {"lang": "es_ES"}},
    )
    lines = await _enrich_lines(session, lines)
    alerts = []
    for l in lines:
        remaining = l.get("remaining_quantity") or 0.0
        used = l.get("used_quantity") or 0.0
        total = remaining + used
        if total <= 0 or remaining < 0:
            continue
        ratio = remaining / total
        if ratio < threshold:
            alerts.append(
                {
                    "line_id": l["id"],
                    "product_name": (l.get("product_id") or [None, ""])[1],
                    "size": l.get("product_size_value") or "",
                    "delegation_name": (l.get("sol_delegation_id") or [None, ""])[1],
                    "dept_name": l.get("dept_name") or "",
                    "remaining": remaining,
                    "total": total,
                    "ratio": ratio,
                }
            )
    alerts.sort(key=lambda a: a["ratio"])
    return {"rows": alerts[:limit], "total": len(alerts)}


# ---------------------------------------------------------------------------
# Acuerdos
# ---------------------------------------------------------------------------


@app.get("/api/agreements")
async def list_agreements(session: SessionDep):
    return await _call_kw(
        session,
        "uniform.agreement",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "name",
                "partner_id",
                "state",
                "date_start",
                "date_end",
                "department_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/agreements/{agreement_id}")
async def get_agreement(agreement_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {
            "fields": [
                "id",
                "name",
                "partner_id",
                "state",
                "date_start",
                "date_end",
                "department_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Acuerdo no encontrado")
    return records[0]


# ---------------------------------------------------------------------------
# Delegaciones (res.partner con is_delegation=True)
# ---------------------------------------------------------------------------


@app.get("/api/delegations")
async def list_all_delegations(session: SessionDep):
    return await _call_kw(
        session,
        "res.partner",
        "search_read",
        [[["is_delegation", "=", True]]],
        {
            "fields": [
                "id",
                "name",
                "city",
                "street",
                "zip",
                "phone",
                "email",
                "partner_latitude",
                "partner_longitude",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/agreements/{agreement_id}/delegations")
async def list_agreement_delegations(agreement_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {"fields": ["id", "delegation_ids"], "context": {"lang": "es_ES"}},
    )
    if not records or not records[0].get("delegation_ids"):
        return []
    return await _call_kw(
        session,
        "res.partner",
        "read",
        [records[0]["delegation_ids"]],
        {
            "fields": ["id", "name", "city", "street", "zip", "phone", "email"],
            "context": {"lang": "es_ES"},
        },
    )


# ---------------------------------------------------------------------------
# Departamentos
# ---------------------------------------------------------------------------


@app.get("/api/departments")
async def list_all_departments(session: SessionDep):
    """Todos los departamentos de todos los acuerdos."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "name",
                "agreement_ids",
                "worker_ids",
                "delegation_ids",
                "partner_id",
                "uniform_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    all_deleg_ids = list({did for d in depts for did in d.get("delegation_ids", [])})
    if all_deleg_ids:
        delegs = await _call_kw(
            session,
            "res.partner",
            "read",
            [all_deleg_ids],
            {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
        )
        deleg_name_map = {d["id"]: d["name"] for d in delegs}
        for dept in depts:
            dept["delegation_names"] = [
                deleg_name_map.get(did, str(did))
                for did in dept.get("delegation_ids", [])
            ]
    else:
        for dept in depts:
            dept["delegation_names"] = []
    return depts


@app.get("/api/agreements/{agreement_id}/departments")
async def list_departments(agreement_id: int, session: SessionDep):
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[["agreement_ids", "in", agreement_id]]],
        {
            "fields": [
                "id",
                "name",
                "agreement_ids",
                "worker_ids",
                "delegation_ids",
                "partner_id",
                "uniform_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    all_deleg_ids = list({did for d in depts for did in d.get("delegation_ids", [])})
    if all_deleg_ids:
        delegs = await _call_kw(
            session,
            "res.partner",
            "read",
            [all_deleg_ids],
            {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
        )
        deleg_name_map = {d["id"]: d["name"] for d in delegs}
        for dept in depts:
            dept["delegation_names"] = [
                deleg_name_map.get(did, str(did))
                for did in dept.get("delegation_ids", [])
            ]
    else:
        for dept in depts:
            dept["delegation_names"] = []
    return depts


@app.get("/api/agreements/{agreement_id}/delegations_full")
async def list_delegations_full(agreement_id: int, session: SessionDep):
    """Delegaciones con sus departamentos anidados y estadísticas."""
    ag = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {"fields": ["id", "department_ids"], "context": {"lang": "es_ES"}},
    )
    if not ag:
        return []
    dept_ids = ag[0].get("department_ids", [])
    if not dept_ids:
        return []
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "read",
        [dept_ids],
        {
            "fields": [
                "id",
                "name",
                "delegation_ids",
                "worker_ids",
                "uniform_ids",
                "partner_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    all_deleg_ids = list({did for d in depts for did in d.get("delegation_ids", [])})
    if not all_deleg_ids:
        return []
    delegs_raw = await _call_kw(
        session,
        "res.partner",
        "read",
        [all_deleg_ids],
        {
            "fields": [
                "id",
                "name",
                "street",
                "city",
                "zip",
                "phone",
                "email",
                "vat",
                "parent_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    deleg_map = {
        d["id"]: {
            **d,
            "departments": [],
            "worker_count": 0,
            "dept_count": 0,
            "order_count": 0,
        }
        for d in delegs_raw
    }
    for dept in depts:
        nw = len(dept.get("worker_ids", []))
        no = len(dept.get("uniform_ids", []))
        dept_info = {
            "id": dept["id"],
            "name": dept["name"],
            "worker_count": nw,
            "order_count": no,
            "partner_id": dept.get("partner_id"),
        }
        for did in dept.get("delegation_ids", []):
            if did in deleg_map:
                deleg_map[did]["departments"].append(dept_info)
                deleg_map[did]["worker_count"] += nw
                deleg_map[did]["dept_count"] += 1
                deleg_map[did]["order_count"] += no
    return list(deleg_map.values())


@app.get("/api/departments/{dept_id}")
async def get_department(dept_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "uniform.agreement.department",
        "read",
        [[dept_id]],
        {
            "fields": ["id", "name", "agreement_ids", "worker_ids"],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Departamento no encontrado")
    return records[0]


# ---------------------------------------------------------------------------
# Trabajadores
# ---------------------------------------------------------------------------


@app.get("/api/departments/{dept_id}/workers")
async def list_workers(dept_id: int, session: SessionDep):
    return await _call_kw(
        session,
        "uniform.agreement.worker",
        "search_read",
        [[["department_ids", "in", dept_id]]],
        {
            "fields": ["id", "name", "department_ids", "size_ids"],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/workers/{worker_id}")
async def get_worker(worker_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [[worker_id]],
        {
            "fields": ["id", "name", "department_ids", "size_ids"],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Trabajador no encontrado")
    return records[0]


@app.post("/api/workers")
async def create_worker(request: Request, session: SessionDep):
    body = await request.json()
    new_id = await _call_kw(session, "uniform.agreement.worker", "create", [body])
    # Devolver el trabajador creado con todos sus campos
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [[new_id]],
        {
            "fields": [
                "id",
                "name",
                "code",
                "department_ids",
                "delegation_id",
                "partner_id",
                "related_contact_id",
                "size_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    return workers[0] if workers else {"id": new_id}


@app.get("/api/workers/{worker_id}/lines")
async def get_worker_lines(worker_id: int, session: SessionDep):
    """SOLs donde este trabajador está asignado — para la vista 'Prendas del trabajador'."""
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["worker_ids", "in", worker_id], ["display_type", "=", False]]],
        {"fields": _LINE_FIELDS + ["order_state"], "context": {"lang": "es_ES"}},
    )
    lines = await _enrich_lines(session, lines)
    return lines


@app.get("/api/workers/{worker_id}/deliveries")
async def get_worker_deliveries(worker_id: int, session: SessionDep):
    """Historial de entregas reales (stock.move en done) del trabajador."""
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["worker_ids", "in", worker_id], ["display_type", "=", False]]],
        {
            "fields": ["id", "product_size_value", "product_color_value"],
            "context": {"lang": "es_ES"},
        },
    )
    line_ids = [l["id"] for l in lines]
    if not line_ids:
        return []
    line_map = {l["id"]: l for l in lines}
    moves = await _call_kw(
        session,
        "stock.move",
        "search_read",
        [
            [
                ["sale_line_id", "in", line_ids],
                ["state", "=", "done"],
                ["picking_id", "!=", False],
            ]
        ],
        {
            "fields": [
                "id",
                "product_id",
                "product_uom_qty",
                "date",
                "picking_id",
                "sale_line_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    for m in moves:
        sol = line_map.get(m["sale_line_id"][0] if m.get("sale_line_id") else None, {})
        m["product_size_value"] = sol.get("product_size_value", "")
        m["product_color_value"] = sol.get("product_color_value", "")
    moves.sort(key=lambda m: m.get("date") or "", reverse=True)
    return moves


@app.get("/api/partners/search")
async def search_partners(
    session: SessionDep, q: str = "", is_delegation: bool = False
):
    """Búsqueda de partners para el autocompletado del modal."""
    domain: list = [["active", "=", True]]
    if is_delegation:
        domain.append(["is_delegation", "=", True])
    if q.strip():
        domain.append(["name", "ilike", q.strip()])
    return await _call_kw(
        session,
        "res.partner",
        "search_read",
        [domain],
        {
            "fields": ["id", "name", "email", "city"],
            "limit": 15,
            "context": {"lang": "es_ES"},
        },
    )


@app.put("/api/workers/{worker_id}")
async def update_worker(worker_id: int, request: Request, session: SessionDep):
    body = await request.json()
    await _call_kw(session, "uniform.agreement.worker", "write", [[worker_id], body])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tallas
# ---------------------------------------------------------------------------


@app.get("/api/workers/{worker_id}/sizes")
async def list_worker_sizes(worker_id: int, session: SessionDep):
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [[worker_id]],
        {
            "fields": [
                "id",
                "name",
                "code",
                "department_ids",
                "delegation_id",
                "size_ids",
                "employment_state",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    worker = workers[0] if workers else {}
    sizes = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "search_read",
        [[["worker_id", "=", worker_id]]],
        {
            "fields": ["id", "worker_id", "category_id", "size_value_id"],
            "context": {"lang": "es_ES"},
        },
    )
    for s in sizes:
        cat = s.get("category_id")
        sv = s.get("size_value_id")
        s["category_name"] = cat[1] if isinstance(cat, list) else ""
        s["size_value"] = sv[1] if isinstance(sv, list) else ""
        s["size_value_id_id"] = sv[0] if isinstance(sv, list) else sv
    return {"worker": worker, "sizes": sizes}


@app.put("/api/workers/{worker_id}/sizes")
async def update_worker_sizes(worker_id: int, request: Request, session: SessionDep):
    """Actualiza las tallas de un trabajador. Espera lista de {category_id, size_value_id}."""
    body = await request.json()
    existing = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "search_read",
        [[["worker_id", "=", worker_id]]],
        {"fields": ["id"], "context": {"lang": "es_ES"}},
    )
    if existing:
        await _call_kw(
            session,
            "uniform.agreement.worker.size",
            "unlink",
            [[r["id"] for r in existing]],
        )
    for size_data in body.get("sizes", []):
        record = {
            "worker_id": worker_id,
            "category_id": size_data["category_id"],
            "size_value_id": size_data["size_value_id"],
        }
        await _call_kw(session, "uniform.agreement.worker.size", "create", [record])
    return {"ok": True}


@app.patch("/api/sizes/{size_id}")
async def patch_size(size_id: int, request: Request, session: SessionDep):
    body = await request.json()
    sv_id = body.get("size_value_id")
    if sv_id:
        await _call_kw(
            session,
            "uniform.agreement.worker.size",
            "write",
            [[size_id], {"size_value_id": int(sv_id)}],
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------------


@app.get("/api/agreements/{agreement_id}/orders")
async def list_orders(agreement_id: int, session: SessionDep):
    return await _orders_with_stats(
        session, [["uniform_agreement_id", "=", agreement_id]]
    )


@app.post("/api/orders/{order_id}/confirm")
async def confirm_order(order_id: int, session: SessionDep):
    await _call_kw(session, "sale.order", "action_confirm", [[order_id]])
    return {"ok": True}


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: int, session: SessionDep):
    await _call_kw(session, "sale.order", "action_cancel", [[order_id]])
    return {"ok": True}


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {
            "fields": [
                "id",
                "name",
                "state",
                "date_order",
                "amount_total",
                "partner_id",
                "uniform_agreement_id",
                "order_line",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Pedido no encontrado")
    return records[0]


@app.get("/api/orders/{order_id}/deliveries")
async def get_order_deliveries(order_id: int, session: SessionDep):
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {"fields": ["id", "picking_ids"], "context": {"lang": "es_ES"}},
    )
    picking_ids = orders[0].get("picking_ids", []) if orders else []
    if not picking_ids:
        return []
    return await _call_kw(
        session,
        "stock.picking",
        "read",
        [picking_ids],
        {
            "fields": [
                "id",
                "name",
                "state",
                "scheduled_date",
                "date_done",
                "partner_id",
                "picking_type_id",
                "origin",
                "move_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/orders/{order_id}/invoices")
async def get_order_invoices(order_id: int, session: SessionDep):
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {"fields": ["id", "invoice_ids"], "context": {"lang": "es_ES"}},
    )
    invoice_ids = orders[0].get("invoice_ids", []) if orders else []
    if not invoice_ids:
        return []
    return await _call_kw(
        session,
        "account.move",
        "read",
        [invoice_ids],
        {
            "fields": [
                "id",
                "name",
                "state",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "partner_id",
                "payment_state",
                "move_type",
                "invoice_origin",
            ],
            "context": {"lang": "es_ES"},
        },
    )


_LINE_FIELDS = [
    "id",
    "product_id",
    "product_uom_qty",
    "qty_delivered",
    "remaining_quantity",
    "used_quantity",
    "price_unit",
    "price_subtotal",
    "name",
    "order_id",
    "product_size_value",
    "product_color_value",
    "sol_agreement_id",
    "sol_delegation_id",
    "sol_department_ids",
]


async def _enrich_lines(session: dict, lines: list) -> list:
    """Resuelve nombre de departamento para cada línea."""
    if not lines:
        return lines
    # sol_department_ids puede venir como lista de IDs o False
    all_dept_ids = list(
        {
            did
            for l in lines
            for did in (l.get("sol_department_ids") or [])
            if isinstance(did, int)
        }
    )
    dept_name_map: dict = {}
    if all_dept_ids:
        try:
            depts = await _call_kw(
                session,
                "uniform.agreement.department",
                "read",
                [all_dept_ids],
                {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
            )
            dept_name_map = {d["id"]: d["name"] for d in depts}
        except Exception:
            pass
    for l in lines:
        dept_ids = l.get("sol_department_ids") or []
        if isinstance(dept_ids, list):
            l["dept_name"] = (
                ", ".join(
                    dept_name_map.get(d, str(d)) for d in dept_ids if isinstance(d, int)
                )
                or ""
            )
        else:
            l["dept_name"] = ""
    return lines


@app.get("/api/lines")
async def list_all_lines(
    session: SessionDep, page: int = 1, limit: int = 100, search: str = ""
):
    """Todas las líneas de pedido (sin filtro de acuerdo), paginadas. search filtra por nombre de producto."""
    domain: list = [["display_type", "=", False]]
    if search.strip():
        domain.append(["product_id.name", "ilike", search.strip()])
    total = await _call_kw(
        session,
        "sale.order.line",
        "search_count",
        [domain],
        {"context": {"lang": "es_ES"}},
    )
    pages = max(1, -(-total // limit))
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [domain],
        {
            "fields": _LINE_FIELDS,
            "limit": limit,
            "offset": (page - 1) * limit,
            "context": {"lang": "es_ES"},
        },
    )
    lines = await _enrich_lines(session, lines)
    return {"lines": lines, "orders": [], "total": total, "page": page, "pages": pages}


@app.get("/api/agreements/{agreement_id}/lines")
async def list_agreement_lines(
    agreement_id: int,
    session: SessionDep,
    page: int = 1,
    limit: int = 100,
    search: str = "",
):
    """Líneas de los pedidos del acuerdo, paginadas. search filtra por nombre de producto."""
    orders = await _call_kw(
        session,
        "sale.order",
        "search_read",
        [[["uniform_agreement_id", "=", agreement_id]]],
        {"fields": ["id", "amount_total", "state"], "context": {"lang": "es_ES"}},
    )
    order_ids = [o["id"] for o in orders]
    if not order_ids:
        return {"lines": [], "orders": [], "total": 0, "page": page, "pages": 0}
    domain: list = [["order_id", "in", order_ids], ["display_type", "=", False]]
    if search.strip():
        domain.append(["product_id.name", "ilike", search.strip()])
    total = await _call_kw(
        session,
        "sale.order.line",
        "search_count",
        [domain],
        {"context": {"lang": "es_ES"}},
    )
    pages = max(1, -(-total // limit))
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [domain],
        {
            "fields": _LINE_FIELDS,
            "limit": limit,
            "offset": (page - 1) * limit,
            "context": {"lang": "es_ES"},
        },
    )
    lines = await _enrich_lines(session, lines)
    return {
        "lines": lines,
        "orders": orders,
        "total": total,
        "page": page,
        "pages": pages,
    }


@app.get("/api/agreements/{agreement_id}/product_groups")
async def list_product_groups(agreement_id: int, session: SessionDep):
    """Grupos de prendas con cupo compartido por trabajador, de este acuerdo."""
    return await _call_kw(
        session,
        "uniform.agreement.product.group",
        "search_read",
        [[["agreement_id", "=", agreement_id]]],
        {
            "fields": ["id", "name", "product_ids", "max_qty"],
            "context": {"lang": "es_ES"},
        },
    )


@app.post("/api/agreements/{agreement_id}/product_groups")
async def create_product_group(
    agreement_id: int, request: Request, session: SessionDep
):
    """Agrupa 2+ prendas del acuerdo para compartir un máximo de unidades por trabajador."""
    body = await request.json()
    product_ids = [int(x) for x in (body.get("product_ids") or [])]
    if len(product_ids) < 2:
        raise HTTPException(400, "Un grupo necesita al menos 2 prendas")
    max_qty = int(body.get("max_qty") or 1)
    new_id = await _call_kw(
        session,
        "uniform.agreement.product.group",
        "create",
        [
            {
                "agreement_id": agreement_id,
                "product_ids": [[6, 0, product_ids]],
                "max_qty": max_qty,
            }
        ],
    )
    return {"id": new_id}


@app.delete("/api/product_groups/{group_id}")
async def delete_product_group(group_id: int, session: SessionDep):
    await _call_kw(session, "uniform.agreement.product.group", "unlink", [[group_id]])
    return {"ok": True}


@app.post("/api/lines/{line_id}/deliver")
async def deliver_line(line_id: int, request: Request, session: SessionDep):
    """Entrega real: valida el albarán de salida de la línea en Odoo.

    Llama a sale.order.line.action_portal_deliver (edyma_orders_per_worker).
    Body opcional: {"quantity": N} — por defecto entrega todo lo pendiente.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    qty = body.get("quantity")
    args = [[line_id]] if qty in (None, "") else [[line_id], float(qty)]
    return await _call_kw(session, "sale.order.line", "action_portal_deliver", args)


@app.post("/api/lines/{line_id}/return")
async def return_line(line_id: int, request: Request, session: SessionDep):
    """Devolución real: crea y valida un albarán de entrada que revierte una
    entrega previa de la línea.

    Llama a sale.order.line.action_portal_return (edyma_orders_per_worker).
    Body opcional: {"quantity": N, "reason": "..."} — por defecto devuelve
    todo lo entregado.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    qty = body.get("quantity")
    reason = body.get("reason") or None
    quantity_arg = None if qty in (None, "") else float(qty)
    return await _call_kw(
        session,
        "sale.order.line",
        "action_portal_return",
        [[line_id], quantity_arg, reason],
    )


# ---------------------------------------------------------------------------
# Endpoints globales (sin filtro de acuerdo)
# ---------------------------------------------------------------------------


async def _orders_with_stats(session: dict, domain: list) -> list:
    """Reutilizable: busca pedidos con estadísticas de líneas."""
    orders = await _call_kw(
        session,
        "sale.order",
        "search_read",
        [domain],
        {
            "fields": [
                "id",
                "name",
                "state",
                "date_order",
                "amount_total",
                "partner_id",
                "uniform_agreement_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if orders:
        order_ids = [o["id"] for o in orders]
        try:
            groups = await _call_kw(
                session,
                "sale.order.line",
                "read_group",
                [
                    [["order_id", "in", order_ids]],
                    ["product_uom_qty:sum", "qty_delivered:sum"],
                    ["order_id"],
                ],
                {"lazy": False, "context": {"lang": "es_ES"}},
            )
            stats = {g["order_id"][0]: g for g in groups}
            worker_counts: dict = {}
            try:
                lines = await _call_kw(
                    session,
                    "sale.order.line",
                    "search_read",
                    [[["order_id", "in", order_ids], ["display_type", "=", False]]],
                    {
                        "fields": ["order_id", "worker_ids"],
                        "context": {"lang": "es_ES"},
                    },
                )
                by_order: dict = {}
                for l in lines:
                    oid = l["order_id"][0] if l.get("order_id") else None
                    if oid is None:
                        continue
                    by_order.setdefault(oid, set()).update(l.get("worker_ids") or [])
                worker_counts = {oid: len(wids) for oid, wids in by_order.items()}
            except Exception:
                pass
            for o in orders:
                g = stats.get(o["id"], {})
                ped = int(g.get("product_uom_qty", 0) or 0)
                rec = int(g.get("qty_delivered", 0) or 0)
                o["stat_pedidas"] = ped
                o["stat_recibidas"] = rec
                o["stat_pendientes"] = max(0, ped - rec)
                o["stat_workers"] = worker_counts.get(o["id"], 0)
        except Exception:
            pass
    return orders


@app.get("/api/all_orders")
async def list_all_orders(session: SessionDep):
    return await _orders_with_stats(session, [])


async def _enrich_workers_with_stats(session: dict, workers: list) -> list:
    """Añade assigned_count, delivered_count y size_labels a cada trabajador."""
    worker_ids = [w["id"] for w in workers]
    for w in workers:
        w["assigned_count"] = len(w.get("size_ids", []))
        w["delivered_count"] = 0
        w["size_labels"] = []
    if not worker_ids:
        return workers
    try:
        sols = await _call_kw(
            session,
            "sale.order.line",
            "search_read",
            [[["worker_ids", "in", worker_ids], ["display_type", "=", False]]],
            {
                "fields": ["id", "worker_ids", "qty_delivered"],
                "context": {"lang": "es_ES"},
            },
        )
        worker_map = {w["id"]: w for w in workers}
        for sol in sols:
            if (sol.get("qty_delivered") or 0) > 0:
                for wid in sol.get("worker_ids", []):
                    if wid in worker_map:
                        worker_map[wid]["delivered_count"] += 1
    except Exception:
        pass
    try:
        all_sizes = await _call_kw(
            session,
            "uniform.agreement.worker.size",
            "search_read",
            [[["worker_id", "in", worker_ids]]],
            {"fields": ["worker_id", "size_value_id"], "context": {"lang": "es_ES"}},
        )
        wmap = {w["id"]: w for w in workers}
        for sz in all_sizes:
            wid_raw = sz.get("worker_id")
            wid = wid_raw[0] if isinstance(wid_raw, list) else wid_raw
            sv = sz.get("size_value_id")
            label = sv[1] if isinstance(sv, list) and len(sv) > 1 else ""
            if label and wid and wid in wmap:
                wmap[wid]["size_labels"].append(label)
    except Exception:
        pass
    return workers


@app.get("/api/all_workers")
async def list_all_workers_global(session: SessionDep):
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[]],
        {"fields": ["id", "name", "worker_ids"], "context": {"lang": "es_ES"}},
    )
    # Búsqueda directa del modelo (no derivada de department.worker_ids): un
    # trabajador sin departamento asignado (p.ej. tras una importación sin
    # match de departamento) debe seguir siendo visible en el portal.
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "name",
                "code",
                "department_ids",
                "delegation_id",
                "size_ids",
                "employment_state",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    workers = await _enrich_workers_with_stats(session, workers)
    return {"workers": workers, "departments": depts}


@app.get("/api/all_deliveries")
async def list_all_deliveries(session: SessionDep):
    return await _call_kw(
        session,
        "stock.picking",
        "search_read",
        [[["state", "!=", "cancel"], ["picking_type_code", "=", "outgoing"]]],
        {
            "fields": [
                "id",
                "name",
                "state",
                "scheduled_date",
                "date_done",
                "partner_id",
                "picking_type_id",
                "origin",
                "move_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/all_invoices")
async def list_all_invoices(session: SessionDep):
    return await _call_kw(
        session,
        "account.move",
        "search_read",
        [
            [
                ["move_type", "in", ["out_invoice", "out_refund"]],
                ["state", "!=", "cancel"],
            ]
        ],
        {
            "fields": [
                "id",
                "name",
                "state",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "partner_id",
                "payment_state",
                "move_type",
                "invoice_origin",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/delegations_full")
async def list_all_delegations_full(session: SessionDep):
    """Todas las delegaciones con sus departamentos anidados (sin filtro de acuerdo)."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "name",
                "delegation_ids",
                "worker_ids",
                "uniform_ids",
                "partner_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    # Todas las delegaciones (is_delegation=True), no solo las que ya cuelgan de
    # un departamento — si no, una delegación recién creada sin departamento
    # asignado no tiene forma de aparecer en la respuesta.
    delegs_raw = await _call_kw(
        session,
        "res.partner",
        "search_read",
        [[["is_delegation", "=", True]]],
        {
            "fields": ["id", "name", "street", "city", "zip", "phone", "email", "vat"],
            "context": {"lang": "es_ES"},
        },
    )
    deleg_map = {
        d["id"]: {
            **d,
            "departments": [],
            "worker_count": 0,
            "dept_count": 0,
            "order_count": 0,
        }
        for d in delegs_raw
    }
    for dept in depts:
        nw = len(dept.get("worker_ids", []))
        no = len(dept.get("uniform_ids", []))
        dept_info = {
            "id": dept["id"],
            "name": dept["name"],
            "worker_count": nw,
            "order_count": no,
            "partner_id": dept.get("partner_id"),
        }
        for did in dept.get("delegation_ids", []):
            if did in deleg_map:
                deleg_map[did]["departments"].append(dept_info)
                deleg_map[did]["worker_count"] += nw
                deleg_map[did]["dept_count"] += 1
                deleg_map[did]["order_count"] += no
    return list(deleg_map.values())


@app.get("/api/agreements/{agreement_id}/all_workers")
async def list_all_workers(agreement_id: int, session: SessionDep):
    """Todos los trabajadores de todos los departamentos del acuerdo."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[["agreement_ids", "in", agreement_id]]],
        {"fields": ["id", "name", "worker_ids"], "context": {"lang": "es_ES"}},
    )
    all_worker_ids = [wid for d in depts for wid in d.get("worker_ids", [])]
    if not all_worker_ids:
        return {"workers": [], "departments": depts}
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [all_worker_ids],
        {
            "fields": [
                "id",
                "name",
                "code",
                "department_ids",
                "delegation_id",
                "size_ids",
                "employment_state",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    workers = await _enrich_workers_with_stats(session, workers)
    return {"workers": workers, "departments": depts}


@app.get("/api/agreements/{agreement_id}/workers_count")
async def agreement_workers_count(agreement_id: int, session: SessionDep):
    """Cuenta total de trabajadores en todos los departamentos del acuerdo."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[["agreement_ids", "in", agreement_id]]],
        {"fields": ["id", "worker_ids"], "context": {"lang": "es_ES"}},
    )
    total = sum(len(d.get("worker_ids", [])) for d in depts)
    return {"count": total, "departments": len(depts)}


@app.get("/api/orders/{order_id}/lines")
async def list_order_lines(order_id: int, session: SessionDep):
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["order_id", "=", order_id]]],
        {
            "fields": [
                "id",
                "order_id",
                "product_id",
                "product_uom_qty",
                "qty_delivered",
                "price_unit",
                "price_subtotal",
                "worker_ids",
                "name",
                "state",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    # worker_ids es Many2many en el modelo real; el front espera un tuple [id, name]
    # tipo Many2one (un trabajador por línea, uso habitual del asistente de reparto).
    worker_ids = list({wid for l in lines for wid in (l.get("worker_ids") or [])})
    worker_map: dict = {}
    if worker_ids:
        workers = await _call_kw(
            session,
            "uniform.agreement.worker",
            "read",
            [worker_ids],
            {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
        )
        worker_map = {w["id"]: w["name"] for w in workers}
    for l in lines:
        wids = l.get("worker_ids") or []
        l["worker_id"] = [wids[0], worker_map.get(wids[0], "")] if wids else False
    return lines


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------


@app.get("/api/products")
async def list_products(
    session: SessionDep, page: int = 1, limit: int = 100, search: str = ""
):
    """Catálogo completo, paginado por producto (product.template) con sus
    variantes (product.product) anidadas — evita listar cada talla como si
    fuera un producto distinto."""
    domain: list = [["sale_ok", "=", True]]
    if search.strip():
        domain.append(["name", "ilike", search.strip()])
    total = await _call_kw(
        session,
        "product.template",
        "search_count",
        [domain],
        {"context": {"lang": "es_ES"}},
    )
    pages = max(1, -(-total // limit))
    templates = await _call_kw(
        session,
        "product.template",
        "search_read",
        [domain],
        {
            "fields": [
                "id",
                "name",
                "default_code",
                "list_price",
                "categ_id",
                "image_128",
            ],
            "limit": limit,
            "offset": (page - 1) * limit,
            "order": "name",
            "context": {"lang": "es_ES"},
        },
    )
    tmpl_ids = [t["id"] for t in templates]
    variants_by_tmpl: dict = {tid: [] for tid in tmpl_ids}
    if tmpl_ids:
        variants = await _call_kw(
            session,
            "product.product",
            "search_read",
            [[["product_tmpl_id", "in", tmpl_ids]]],
            {
                "fields": ["id", "display_name", "default_code", "product_tmpl_id"],
                "context": {"lang": "es_ES"},
            },
        )
        for v in variants:
            tid = v["product_tmpl_id"][0]
            variants_by_tmpl.setdefault(tid, []).append(v)
    for t in templates:
        t["variants"] = variants_by_tmpl.get(t["id"], [])
    return {"products": templates, "total": total, "page": page, "pages": pages}


@app.get("/api/size_values")
async def list_size_values(session: SessionDep):
    """Todos los valores de atributo de tipo talla."""
    return await _call_kw(
        session,
        "product.attribute.value",
        "search_read",
        [[["attribute_id.type", "=", "size"]]],
        {"fields": ["id", "name", "attribute_id"], "context": {"lang": "es_ES"}},
    )


@app.get("/api/products/{product_id}")
async def get_product(product_id: int, session: SessionDep):
    # product_id is a product.product (variant) ID — resolve to template
    variant = await _call_kw(
        session,
        "product.product",
        "read",
        [[product_id]],
        {"fields": ["id", "product_tmpl_id"], "context": {"lang": "es_ES"}},
    )
    if not variant:
        raise HTTPException(404, "Variante no encontrada")
    tmpl_raw = variant[0].get("product_tmpl_id")
    tmpl_id = tmpl_raw[0] if isinstance(tmpl_raw, list) else tmpl_raw
    records = await _call_kw(
        session,
        "product.template",
        "read",
        [[tmpl_id]],
        {
            "fields": [
                "id",
                "name",
                "default_code",
                "description_sale",
                "list_price",
                "categ_id",
                "attribute_line_ids",
                "image_512",
                "barcode",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Producto no encontrado")
    rec = records[0]
    rec["variant_id"] = product_id
    line_ids = rec.get("attribute_line_ids") or []
    size_values: list = []
    if line_ids:
        lines = await _call_kw(
            session,
            "product.template.attribute.line",
            "read",
            [line_ids],
            {"fields": ["attribute_id", "value_ids"], "context": {"lang": "es_ES"}},
        )
        attr_ids = list({l["attribute_id"][0] for l in lines if l.get("attribute_id")})
        attrs = await _call_kw(
            session,
            "product.attribute",
            "read",
            [attr_ids],
            {"fields": ["id", "type"], "context": {"lang": "es_ES"}},
        )
        size_attr_ids = {a["id"] for a in attrs if a.get("type") == "size"}
        value_ids = list(
            {
                vid
                for l in lines
                if l.get("attribute_id") and l["attribute_id"][0] in size_attr_ids
                for vid in l.get("value_ids", [])
            }
        )
        if value_ids:
            values = await _call_kw(
                session,
                "product.attribute.value",
                "read",
                [value_ids],
                {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
            )
            size_values = [{"id": v["id"], "name": v["name"]} for v in values]
    rec["size_values"] = size_values
    return rec


@app.get("/api/products/{product_id}/usage")
async def get_product_usage(product_id: int, session: SessionDep):
    """Estadísticas y pedidos que contienen esta plantilla de producto.
    product_id es un ID de product.product (variante)."""
    # Resolve variant → template → all variants of that template
    variant_info = await _call_kw(
        session,
        "product.product",
        "read",
        [[product_id]],
        {"fields": ["product_tmpl_id"], "context": {"lang": "es_ES"}},
    )
    if not variant_info:
        return {"total": 0, "pedidas": 0, "entregadas": 0, "orders": []}
    tmpl_raw = variant_info[0].get("product_tmpl_id")
    tmpl_id = tmpl_raw[0] if isinstance(tmpl_raw, list) else tmpl_raw
    variants = await _call_kw(
        session,
        "product.product",
        "search_read",
        [[["product_tmpl_id", "=", tmpl_id]]],
        {"fields": ["id"], "context": {"lang": "es_ES"}},
    )
    variant_ids = [v["id"] for v in variants]
    if not variant_ids:
        return {"total": 0, "pedidas": 0, "entregadas": 0, "orders": []}
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["product_id", "in", variant_ids], ["display_type", "=", False]]],
        {
            "fields": [
                "id",
                "order_id",
                "product_uom_qty",
                "qty_delivered",
                "sol_agreement_id",
                "sol_delegation_id",
                "sol_department_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    total_ped = sum((l.get("product_uom_qty") or 0) for l in lines)
    total_ent = sum((l.get("qty_delivered") or 0) for l in lines)
    order_map: dict = {}
    for l in lines:
        if not l.get("order_id"):
            continue
        oid = l["order_id"][0]
        if oid not in order_map:
            order_map[oid] = {
                "order_name": l["order_id"][1],
                "agreement": l.get("sol_agreement_id"),
                "delegation": l.get("sol_delegation_id"),
                "dept_ids": [],
                "pedidas": 0,
                "entregadas": 0,
            }
        order_map[oid]["pedidas"] += l.get("product_uom_qty") or 0
        order_map[oid]["entregadas"] += l.get("qty_delivered") or 0
        dept_ids = l.get("sol_department_ids") or []
        if isinstance(dept_ids, list):
            order_map[oid]["dept_ids"].extend(dept_ids)
    all_dept_ids = list({d for o in order_map.values() for d in o["dept_ids"]})
    dept_names: dict = {}
    if all_dept_ids:
        try:
            depts = await _call_kw(
                session,
                "uniform.agreement.department",
                "read",
                [all_dept_ids],
                {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
            )
            dept_names = {d["id"]: d["name"] for d in depts}
        except Exception:
            pass
    orders = []
    for o in order_map.values():
        ag = o["agreement"]
        deleg = o["delegation"]
        dept_str = ", ".join(dept_names.get(d, str(d)) for d in set(o["dept_ids"]))
        orders.append(
            {
                "order_name": o["order_name"],
                "agreement": ag[1] if isinstance(ag, list) else (ag or ""),
                "delegation": deleg[1] if isinstance(deleg, list) else (deleg or ""),
                "dept": dept_str,
                "total": int(o["pedidas"]),
                "pedidas": int(o["pedidas"]),
                "entregadas": int(o["entregadas"]),
            }
        )
    return {
        "total": int(total_ped),
        "pedidas": int(total_ped),
        "entregadas": int(total_ent),
        "orders": orders,
    }


@app.get("/api/agreements/{agreement_id}/products/{product_id}/workers")
async def get_product_workers(agreement_id: int, product_id: int, session: SessionDep):
    """Trabajadores asignados a una prenda (product.product) en un acuerdo."""
    sols = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [
            [
                ["order_id.uniform_agreement_id", "=", agreement_id],
                ["product_id", "=", product_id],
                ["display_type", "=", False],
            ]
        ],
        {
            "fields": [
                "id",
                "order_id",
                "product_uom_qty",
                "qty_delivered",
                "worker_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    all_worker_ids = list({wid for sol in sols for wid in sol.get("worker_ids", [])})
    if not all_worker_ids:
        return []
    workers_raw = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [all_worker_ids],
        {
            "fields": ["id", "name", "department_ids", "size_ids"],
            "context": {"lang": "es_ES"},
        },
    )
    worker_map = {w["id"]: w for w in workers_raw}
    dept_ids = list({did for w in workers_raw for did in w.get("department_ids", [])})
    dept_names: dict = {}
    if dept_ids:
        depts = await _call_kw(
            session,
            "uniform.agreement.department",
            "read",
            [dept_ids],
            {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
        )
        dept_names = {d["id"]: d["name"] for d in depts}
    categ_id = None
    try:
        prod_info = await _call_kw(
            session,
            "product.product",
            "read",
            [[product_id]],
            {"fields": ["categ_id"], "context": {"lang": "es_ES"}},
        )
        if prod_info:
            raw_cat = prod_info[0].get("categ_id")
            categ_id = raw_cat[0] if isinstance(raw_cat, list) else raw_cat
    except Exception:
        pass
    all_size_ids = list({sid for w in workers_raw for sid in w.get("size_ids", [])})
    size_by_worker: dict = {}
    if all_size_ids and categ_id:
        try:
            sizes = await _call_kw(
                session,
                "uniform.agreement.worker.size",
                "read",
                [all_size_ids],
                {
                    "fields": ["id", "worker_id", "category_id", "size_value_id"],
                    "context": {"lang": "es_ES"},
                },
            )
            for sz in sizes:
                cid = (
                    sz["category_id"][0]
                    if isinstance(sz.get("category_id"), list)
                    else sz.get("category_id")
                )
                if cid != categ_id:
                    continue
                wid = (
                    sz["worker_id"][0]
                    if isinstance(sz.get("worker_id"), list)
                    else sz.get("worker_id")
                )
                sval = (
                    sz["size_value_id"][1]
                    if isinstance(sz.get("size_value_id"), list)
                    else None
                )
                size_by_worker[wid] = sval
        except Exception:
            pass
    result = []
    seen: set = set()
    for sol in sols:
        order_info = sol.get("order_id", [None, "—"])
        order_name = (
            order_info[1]
            if isinstance(order_info, list) and len(order_info) > 1
            else "—"
        )
        for wid in sol.get("worker_ids", []):
            key = (sol["id"], wid)
            if key in seen:
                continue
            seen.add(key)
            w = worker_map.get(wid, {})
            dept_list = w.get("department_ids", [])
            dept_name = dept_names.get(dept_list[0], "—") if dept_list else "—"
            result.append(
                {
                    "worker_id": wid,
                    "worker_name": w.get("name", "?"),
                    "dept_name": dept_name,
                    "talla": size_by_worker.get(wid),
                    "order_name": order_name,
                    "asig": sol.get("product_uom_qty") or 0,
                    "ped": sol.get("product_uom_qty") or 0,
                    "rec": sol.get("qty_delivered") or 0,
                    "ent": sol.get("qty_delivered") or 0,
                }
            )
    return result


# ---------------------------------------------------------------------------
# Albaranes
# ---------------------------------------------------------------------------


@app.get("/api/agreements/{agreement_id}/deliveries")
async def list_deliveries(agreement_id: int, session: SessionDep):
    orders = await _call_kw(
        session,
        "sale.order",
        "search_read",
        [[["uniform_agreement_id", "=", agreement_id]]],
        {"fields": ["id", "picking_ids"], "context": {"lang": "es_ES"}},
    )
    picking_ids = [p for o in orders for p in o.get("picking_ids", [])]
    if not picking_ids:
        return []
    return await _call_kw(
        session,
        "stock.picking",
        "read",
        [picking_ids],
        {
            "fields": [
                "id",
                "name",
                "state",
                "scheduled_date",
                "date_done",
                "partner_id",
                "picking_type_id",
                "origin",
                "move_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/deliveries/{picking_id}/pdf")
async def get_delivery_pdf(picking_id: int, session: SessionDep):
    url = f"{ODOO_URL}/report/pdf/stock.report_deliveryslip/{picking_id}"
    cookies = {"session_id": session["odoo_session_id"]}
    # Cliente aislado (no el _client compartido): su cookie-jar acumula
    # session_id de todos los logins de la app y puede colisionar con el
    # que pasamos aquí, sirviendo el PDF de la sesión equivocada.
    async with httpx.AsyncClient(timeout=30.0) as report_client:
        r = await report_client.get(url, cookies=cookies)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise HTTPException(502, "No se pudo generar el PDF del albarán")
    return Response(
        content=r.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="albaran_{picking_id}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Facturas
# ---------------------------------------------------------------------------


@app.get("/api/agreements/{agreement_id}/invoices")
async def list_invoices(agreement_id: int, session: SessionDep):
    orders = await _call_kw(
        session,
        "sale.order",
        "search_read",
        [[["uniform_agreement_id", "=", agreement_id]]],
        {"fields": ["id", "invoice_ids"], "context": {"lang": "es_ES"}},
    )
    invoice_ids = [i for o in orders for i in o.get("invoice_ids", [])]
    if not invoice_ids:
        return []
    return await _call_kw(
        session,
        "account.move",
        "read",
        [invoice_ids],
        {
            "fields": [
                "id",
                "name",
                "state",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "partner_id",
                "payment_state",
                "move_type",
                "invoice_origin",
            ],
            "context": {"lang": "es_ES"},
        },
    )


# ---------------------------------------------------------------------------
# Detalle de factura
# ---------------------------------------------------------------------------


@app.get("/api/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, session: SessionDep):
    moves = await _call_kw(
        session,
        "account.move",
        "read",
        [[invoice_id]],
        {
            "fields": [
                "id",
                "name",
                "state",
                "invoice_date",
                "invoice_date_due",
                "amount_untaxed",
                "amount_tax",
                "amount_total",
                "amount_residual",
                "partner_id",
                "payment_state",
                "move_type",
                "narration",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not moves:
        raise HTTPException(status_code=404, detail="Invoice not found")
    move = moves[0]
    lines = await _call_kw(
        session,
        "account.move.line",
        "search_read",
        [
            [
                ["move_id", "=", invoice_id],
                ["display_type", "in", ["product", "line_section", "line_note"]],
            ]
        ],
        {
            "fields": [
                "id",
                "product_id",
                "name",
                "quantity",
                "price_unit",
                "discount",
                "price_subtotal",
                "tax_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    move["lines"] = lines
    return move


@app.get("/api/invoices/{invoice_id}/pdf")
async def get_invoice_pdf(invoice_id: int, session: SessionDep):
    url = f"{ODOO_URL}/report/pdf/account.report_invoice_with_payments/{invoice_id}"
    cookies = {"session_id": session["odoo_session_id"]}
    # Cliente aislado: ver comentario en get_delivery_pdf.
    async with httpx.AsyncClient(timeout=30.0) as report_client:
        r = await report_client.get(url, cookies=cookies)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise HTTPException(502, "No se pudo generar el PDF de la factura")
    return Response(
        content=r.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="factura_{invoice_id}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Partner / Perfil
# ---------------------------------------------------------------------------


@app.get("/api/partners/{partner_id}")
async def get_partner(partner_id: int, session: SessionDep):
    records = await _call_kw(
        session,
        "res.partner",
        "read",
        [[partner_id]],
        {
            "fields": [
                "id",
                "name",
                "email",
                "phone",
                "mobile",
                "street",
                "city",
                "zip",
                "country_id",
                "vat",
                "image_128",
                "function",
                "company_id",
                "company_name",
                "parent_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Partner no encontrado")
    return records[0]


@app.put("/api/partners/{partner_id}")
async def update_partner(partner_id: int, request: Request, session: SessionDep):
    body = await request.json()
    allowed = {
        "name",
        "email",
        "phone",
        "mobile",
        "street",
        "city",
        "zip",
        "function",
        "image_1920",
    }
    filtered = {k: v for k, v in body.items() if k in allowed}
    await _call_kw(session, "res.partner", "write", [[partner_id], filtered])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Búsqueda genérica (lista blanca de modelos)
# ---------------------------------------------------------------------------

ALLOWED_MODELS = {
    "uniform.agreement",
    "uniform.agreement.department",
    "uniform.agreement.worker",
    "uniform.agreement.worker.size",
    "sale.order",
    "sale.order.line",
    "stock.picking",
    "account.move",
    "res.partner",
    "product.template",
    "product.product",
    "mrp.production",
}


@app.post("/api/search")
async def generic_search(request: Request, session: SessionDep):
    body = await request.json()
    model = body.get("model", "")
    if model not in ALLOWED_MODELS:
        raise HTTPException(403, f"Modelo '{model}' no permitido")
    return await _call_kw(
        session,
        model,
        "search_read",
        [body.get("domain", [])],
        {
            "fields": body.get("fields", []),
            "limit": min(body.get("limit", 80), 500),
            "context": {"lang": "es_ES"},
        },
    )


# ---------------------------------------------------------------------------
# Delegaciones — creación
# ---------------------------------------------------------------------------


@app.post("/api/delegations")
async def create_delegation(request: Request, session: SessionDep):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "El nombre es obligatorio")
    fields: dict = {"name": name, "is_delegation": True, "type": "delegation"}
    for f in ("street", "city", "zip", "email", "phone", "vat"):
        v = (body.get(f) or "").strip()
        if v:
            fields[f] = v
    new_id = await _call_kw(session, "res.partner", "create", [fields])
    partners = await _call_kw(
        session,
        "res.partner",
        "read",
        [[new_id]],
        {
            "fields": [
                "id",
                "name",
                "city",
                "street",
                "zip",
                "phone",
                "email",
                "is_delegation",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    return partners[0] if partners else {"id": new_id}


# ---------------------------------------------------------------------------
# Departamentos — creación
# ---------------------------------------------------------------------------


@app.post("/api/departments")
async def create_department(request: Request, session: SessionDep):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "El nombre es obligatorio")
    fields: dict = {"name": name}
    deleg_ids = body.get("delegation_ids") or []
    if deleg_ids:
        fields["delegation_ids"] = [[6, 0, [int(x) for x in deleg_ids]]]
    agreement_ids = body.get("agreement_ids") or []
    if agreement_ids:
        fields["agreement_ids"] = [[6, 0, [int(x) for x in agreement_ids]]]
    new_id = await _call_kw(session, "uniform.agreement.department", "create", [fields])
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "read",
        [[new_id]],
        {
            "fields": [
                "id",
                "name",
                "delegation_ids",
                "worker_ids",
                "uniform_ids",
                "partner_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    return depts[0] if depts else {"id": new_id}


@app.put("/api/departments/{dept_id}")
async def update_department(dept_id: int, request: Request, session: SessionDep):
    body = await request.json()
    await _call_kw(session, "uniform.agreement.department", "write", [[dept_id], body])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pedidos — creación (dentro de un acuerdo)
# ---------------------------------------------------------------------------


@app.post("/api/agreements/{agreement_id}/orders")
async def create_order(agreement_id: int, request: Request, session: SessionDep):
    ags = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {"fields": ["id", "partner_id"], "context": {"lang": "es_ES"}},
    )
    if not ags:
        raise HTTPException(404, "Acuerdo no encontrado")
    partner_raw = ags[0].get("partner_id")
    partner_id = partner_raw[0] if isinstance(partner_raw, list) else partner_raw
    if not partner_id:
        raise HTTPException(400, "El acuerdo no tiene cliente asignado")
    fields: dict = {
        "partner_id": partner_id,
        "uniform_agreement_id": agreement_id,
    }
    new_id = await _call_kw(session, "sale.order", "create", [fields])
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[new_id]],
        {
            "fields": [
                "id",
                "name",
                "state",
                "date_order",
                "amount_total",
                "partner_id",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    return orders[0] if orders else {"id": new_id}


# ---------------------------------------------------------------------------
# Importación masiva de trabajadores — recibe JSON parseado por SheetJS
# ---------------------------------------------------------------------------


@app.post("/api/import/workers")
async def import_workers(request: Request, session: SessionDep):
    """Filas ya normalizadas por el front (paso de mapeo de columnas, al
    estilo del asistente de importación de Odoo): cada fila trae las claves
    'name' (obligatoria), 'code' (opcional, se respeta si viene informado;
    si no, el modelo la autogenera por secuencia) y 'department_ids_text'
    (opcional, uno o varios nombres de departamento separados por coma,
    igual que el asistente nativo de Odoo)."""
    body = await request.json()
    rows = body.get("rows") or []
    if not rows:
        raise HTTPException(400, "No hay filas para importar")
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[]],
        {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
    )
    dept_map = {d["name"].strip().lower(): d["id"] for d in depts}
    created = 0
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        name = (str(row.get("name") or row.get("Nombre") or "")).strip()
        if not name:
            errors.append(f"Fila {i}: nombre vacío, saltada")
            continue
        fields: dict = {"name": name}
        code = (
            str(row.get("code") or row.get("Código") or row.get("Codigo") or "")
        ).strip()
        if code:
            fields["code"] = code
        dept_text = (
            str(
                row.get("department_ids_text")
                or row.get("Departamentos")
                or row.get("department")
                or row.get("Departamento")
                or ""
            )
        ).strip()
        if dept_text:
            dept_ids = []
            missing = []
            for dn in dept_text.split(","):
                dn = dn.strip()
                if not dn:
                    continue
                dept_id = dept_map.get(dn.lower())
                if dept_id:
                    dept_ids.append(dept_id)
                else:
                    missing.append(dn)
            if dept_ids:
                fields["department_ids"] = [[6, 0, dept_ids]]
            if missing:
                errors.append(f"Fila {i}: departamento(s) {missing} no encontrado(s)")
        try:
            await _call_kw(session, "uniform.agreement.worker", "create", [fields])
            created += 1
        except Exception as exc:
            errors.append(f"Fila {i} ({name}): {exc}")
    return {"created": created, "errors": errors}


# ---------------------------------------------------------------------------
# Importación masiva de líneas de pedido (tallas/cantidades por trabajador)
# ---------------------------------------------------------------------------


async def _upsert_order_line(
    session: dict,
    order_id: int,
    product_id: int,
    quantity: float,
    worker_id: int | None = None,
    name: str | None = None,
) -> dict:
    """Crea o actualiza la sale.order.line de (order_id, product_id, worker_id).

    Mismo shape de campos que ya usaba el importador Excel (order_id,
    product_id, product_uom_qty, name, worker_ids=[[6,0,[id]]]), pero
    evita duplicar líneas si ya existe una para la misma combinación
    (el importador anterior siempre hacía create, incluso reimportando
    el mismo Excel dos veces).
    """
    domain = [["order_id", "=", order_id], ["product_id", "=", product_id]]
    existing = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [domain],
        {"fields": ["id", "worker_ids", "qty_delivered"], "context": {"lang": "es_ES"}},
    )
    match = None
    for l in existing:
        wids = l.get("worker_ids") or []
        if worker_id and wids == [worker_id]:
            match = l
            break
        if not worker_id and not wids:
            match = l
            break
    if match:
        if quantity <= 0 and (match.get("qty_delivered") or 0) <= 0:
            await _call_kw(session, "sale.order.line", "unlink", [[match["id"]]])
            return {"line_id": None, "quantity": 0.0}
        # Clamp to 0 if quantity is <= 0 (can't unlink due to qty_delivered > 0)
        write_quantity = max(quantity, 0)
        await _call_kw(
            session,
            "sale.order.line",
            "write",
            [[match["id"]], {"product_uom_qty": write_quantity}],
        )
        return {"line_id": match["id"], "quantity": write_quantity}
    if quantity <= 0:
        return {"line_id": None, "quantity": 0.0}
    fields: dict = {
        "order_id": order_id,
        "product_id": product_id,
        "product_uom_qty": quantity,
        "name": name or "",
    }
    if worker_id:
        fields["worker_ids"] = [[6, 0, [worker_id]]]
    new_id = await _call_kw(session, "sale.order.line", "create", [fields])
    return {"line_id": new_id, "quantity": quantity}


async def _template_size_options(session: dict, product_tmpl_id: int) -> list[dict]:
    """Valores de talla disponibles para un product.template. Mismo criterio
    que GET /api/products/{id} (main.py): busca el atributo con
    attribute_id.type == 'size' entre las líneas de atributo del template."""
    tmpl = await _call_kw(
        session,
        "product.template",
        "read",
        [[product_tmpl_id]],
        {"fields": ["attribute_line_ids"], "context": {"lang": "es_ES"}},
    )
    if not tmpl:
        return []
    line_ids = tmpl[0].get("attribute_line_ids") or []
    if not line_ids:
        return []
    lines = await _call_kw(
        session,
        "product.template.attribute.line",
        "read",
        [line_ids],
        {"fields": ["attribute_id", "value_ids"], "context": {"lang": "es_ES"}},
    )
    attr_ids = list({l["attribute_id"][0] for l in lines if l.get("attribute_id")})
    if not attr_ids:
        return []
    attrs = await _call_kw(
        session,
        "product.attribute",
        "read",
        [attr_ids],
        {"fields": ["id", "type"], "context": {"lang": "es_ES"}},
    )
    size_attr_ids = {a["id"] for a in attrs if a.get("type") == "size"}
    value_ids = list(
        {
            vid
            for l in lines
            if l.get("attribute_id") and l["attribute_id"][0] in size_attr_ids
            for vid in l.get("value_ids", [])
        }
    )
    if not value_ids:
        return []
    values = await _call_kw(
        session,
        "product.attribute.value",
        "read",
        [value_ids],
        {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
    )
    return [{"id": v["id"], "name": v["name"]} for v in values]


async def _resolve_variant(
    session: dict,
    product_tmpl_id: int,
    size_value_id: int,
    reference_product_id: int | None = None,
) -> dict:
    """Devuelve la variante (product.product) de product_tmpl_id con la
    talla size_value_id, manteniendo el resto de atributos (p.ej. color)
    iguales a reference_product_id si se indica. Mismo criterio que
    divide_by_workers_wizard._get_variant_from_base_product
    (odoo17_myuniform/custom_addons/edyma_myuniform/wizards/divide_by_workers_wizard.py:72),
    reimplementado como lookup de solo lectura (no se toca el módulo Odoo)."""
    variants = await _call_kw(
        session,
        "product.product",
        "search_read",
        [[["product_tmpl_id", "=", product_tmpl_id]]],
        {
            "fields": ["id", "product_template_attribute_value_ids", "qty_available"],
            "context": {"lang": "es_ES"},
        },
    )
    if not variants:
        raise HTTPException(404, "Sin variantes para esta prenda")
    ptav_ids = list(
        {pid for v in variants for pid in v["product_template_attribute_value_ids"]}
    )
    ptavs = await _call_kw(
        session,
        "product.template.attribute.value",
        "read",
        [ptav_ids],
        {"fields": ["product_attribute_value_id"], "context": {"lang": "es_ES"}},
    )
    ptav_to_value = {p["id"]: p["product_attribute_value_id"][0] for p in ptavs}
    size_ptav_ids = {pid for pid, vid in ptav_to_value.items() if vid == size_value_id}
    candidates = [
        v
        for v in variants
        if size_ptav_ids & set(v["product_template_attribute_value_ids"])
    ]
    if not candidates:
        raise HTTPException(404, "No existe variante para esa talla")
    if reference_product_id:
        ref = next((v for v in variants if v["id"] == reference_product_id), None)
        if ref:
            ref_other = set(ref["product_template_attribute_value_ids"]) - size_ptav_ids
            best = max(
                candidates,
                key=lambda v: len(
                    ref_other
                    & (set(v["product_template_attribute_value_ids"]) - size_ptav_ids)
                ),
            )
            return {
                "product_id": best["id"],
                "qty_available": best.get("qty_available", 0),
            }
    chosen = candidates[0]
    return {"product_id": chosen["id"], "qty_available": chosen.get("qty_available", 0)}


@app.post("/api/orders/{order_id}/lines/import")
async def import_order_lines(order_id: int, request: Request, session: SessionDep):
    body = await request.json()
    rows = body.get("rows") or []
    if not rows:
        raise HTTPException(400, "No hay filas para importar")
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {"fields": ["id"], "context": {"lang": "es_ES"}},
    )
    if not orders:
        raise HTTPException(404, "Pedido no encontrado")
    products = await _call_kw(
        session,
        "product.product",
        "search_read",
        [[["sale_ok", "=", True]]],
        {
            "fields": ["id", "display_name", "default_code"],
            "limit": 1000,
            "context": {"lang": "es_ES"},
        },
    )
    product_map: dict = {}
    for p in products:
        product_map[p["display_name"].strip().lower()] = p["id"]
        if p.get("default_code"):
            product_map[p["default_code"].strip().lower()] = p["id"]
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "search_read",
        [[]],
        {"fields": ["id", "name"], "limit": 2000, "context": {"lang": "es_ES"}},
    )
    worker_map = {w["name"].strip().lower(): w["id"] for w in workers}
    created = 0
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        prod_name = str(
            row.get("Prenda") or row.get("product") or row.get("Producto") or ""
        ).strip()
        if not prod_name:
            errors.append(f"Fila {i}: prenda vacía, saltada")
            continue
        product_id = product_map.get(prod_name.lower())
        if not product_id:
            errors.append(f"Fila {i}: prenda '{prod_name}' no encontrada, saltada")
            continue
        qty_raw = row.get("Cantidad", row.get("quantity", row.get("qty", 1)))
        try:
            qty = float(qty_raw) if qty_raw not in (None, "") else 1.0
        except (TypeError, ValueError):
            qty = 1.0
        worker_name = str(
            row.get("Trabajador") or row.get("worker") or row.get("Empleado") or ""
        ).strip()
        worker_id = None
        if worker_name:
            worker_id = worker_map.get(worker_name.lower())
            if not worker_id:
                errors.append(
                    f"Fila {i}: trabajador '{worker_name}' no encontrado (línea creada sin trabajador)"
                )
        try:
            await _upsert_order_line(
                session, order_id, product_id, qty, worker_id, prod_name
            )
            created += 1
        except Exception as exc:
            errors.append(f"Fila {i} ({prod_name}): {exc}")
    return {"created": created, "errors": errors}


# ---------------------------------------------------------------------------
# Ficheros estáticos y SPA fallback (DEBE ir al final)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")


_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


@app.get("/")
async def root():
    return FileResponse("static/index.html", headers=_NO_CACHE)


@app.get("/{path:path}")
async def spa_fallback(path: str):
    return FileResponse("static/index.html", headers=_NO_CACHE)
