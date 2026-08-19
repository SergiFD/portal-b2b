# -*- coding: utf-8 -*-
"""
Portal B2B — backend FastAPI
Proxy entre el HTML del portal y la API JSON-RPC de Odoo 17.
Cada usuario se autentica con sus propias credenciales de Odoo.
No hay cuenta de servicio hardcodeada.
"""

import html
import os
import secrets
import httpx
from contextlib import asynccontextmanager
from typing import Any, Annotated

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

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


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Sin esto, cualquier excepción no controlada (bug real, timeout de RPC
    contra Odoo, una reconexión a medio reiniciar el contenedor...) la
    devuelve Starlette como texto plano "Internal Server Error" — el
    frontend hace siempre `await r.json()` y eso revienta con
    "SyntaxError: Unexpected token 'I', "Internal S"... is not valid JSON".
    Aquí se homogeneiza a JSON como cualquier HTTPException normal."""
    return JSONResponse(status_code=500, content={"detail": f"Error interno: {exc}"})


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


def _require_role(session: dict, *allowed: str) -> None:
    """Gating de roles a nivel de endpoint (Responsable de Delegación /
    Administrador MY Uniform / Solo consulta / Usuario de almacén).

    'admin' siempre pasa. Los roles se calculan al hacer login llamando a
    res.users.get_my_uniform_role() (edyma_myuniform) y se guardan en la
    sesión del portal; ver login() más abajo.
    """
    role = session.get("role") or "delegation_manager"
    if role == "admin":
        return
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail="Tu rol de usuario no tiene permiso para realizar esta acción.",
        )


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

    role = "delegation_manager"
    try:
        role = await _rpc(
            "/web/dataset/call_kw",
            {
                "model": "res.users",
                "method": "get_my_uniform_role",
                "args": [[result["uid"]]],
                "kwargs": {},
            },
            odoo_session_id=result.get("session_id"),
        )
    except Exception:
        # Si el método aún no existe (módulo no actualizado) mantenemos el
        # comportamiento homogéneo de siempre en vez de romper el login.
        pass

    token = secrets.token_urlsafe(32)
    partner_raw = result.get("partner_id")
    if isinstance(partner_raw, list):
        partner_id = partner_raw[0]
    else:
        partner_id = partner_raw

    commercial_partner_id = partner_id
    try:
        if partner_id:
            partner_data = await _rpc(
                "/web/dataset/call_kw",
                {
                    "model": "res.partner",
                    "method": "read",
                    "args": [[partner_id], ["commercial_partner_id"]],
                    "kwargs": {"context": {"lang": "es_ES"}},
                },
                odoo_session_id=result.get("session_id"),
            )
            if isinstance(partner_data, list) and partner_data:
                commercial_partner_raw = partner_data[0].get("commercial_partner_id")
                if isinstance(commercial_partner_raw, list):
                    commercial_partner_id = commercial_partner_raw[0]
                else:
                    commercial_partner_id = commercial_partner_raw
    except Exception:
        pass

    _sessions[token] = {
        "uid": result["uid"],
        "odoo_session_id": result.get("session_id"),
        "name": result.get("name"),
        "partner_id": partner_id,
        "commercial_partner_id": commercial_partner_id,
        "login": body.get("login"),
        "role": role,
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
        "role": role,
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
        "role": session.get("role") or "delegation_manager",
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


def _portal_list_kwargs(limit: int, sort: str, default_order: str) -> dict:
    """`limit`<=0 = sin límite (se omite la clave, Odoo devuelve todo). `sort`
    solo se acepta si es exactamente 'date desc'/'date asc' (evita pasar
    cualquier cadena arbitraria como `order` a search_read); si no, se usa
    `default_order`."""
    kwargs: dict = {}
    if limit > 0:
        kwargs["limit"] = limit
    kwargs["order"] = sort if sort in ("date desc", "date asc") else default_order
    return kwargs


@app.get("/api/portal_messages")
async def list_portal_messages(
    session: SessionDep, limit: int = 20, sort: str = "date desc"
):
    """Mensajes de la gestora de cuenta para el panel de bienvenida (v-dash).
    El ir.rule del modelo ya filtra a mensajes generales + los del propio cliente."""
    return await _call_kw(
        session,
        "uniform.portal.message",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "body",
                "date",
                "partner_id",
                "title",
                "msg_type",
                "author_name",
            ],
            **_portal_list_kwargs(limit, sort, "date desc"),
            "context": {"lang": "es_ES"},
        },
    )


@app.get("/api/portal_promos")
async def list_portal_promos(
    session: SessionDep, limit: int = 20, sort: str = "date desc"
):
    """Promociones/novedades de MyUniform para el panel Portal.
    Se devuelven promociones activas y las específicas del cliente (partner_id vacío = global).

    Las promociones configuradas por acuerdo (uniform.agreement.portal_promo)
    se deshabilitaron a petición de negocio (2026-07-31): ahora solo existe
    esta fuente, uniform.portal.promo.

    El ir.rule uniform_portal_promo_portal_rule ya filtra por
    user.commercial_partner_id para el grupo portal (general + propio) y no
    aplica a Administrador MY Uniform (base.group_user), que ve todas. No
    dupliques ese filtro aquí en Python: session["partner_id"] es el partner
    de login (para un admin interno, el suyo propio, no el de ningún
    cliente), así que un dominio adicional aquí solo puede estrechar de más
    lo que Odoo ya filtra correctamente."""
    domain = [["active", "=", True]]
    promos = await _call_kw(
        session,
        "uniform.portal.promo",
        "search_read",
        [domain],
        {
            "fields": [
                "id",
                "title",
                "subtitle",
                "image_128",
                "color_from",
                "color_to",
                "cta_show_button",
                "agreement_id",
                "date",
            ],
            **_portal_list_kwargs(limit, sort, "sequence, date desc"),
            "context": {"lang": "es_ES"},
        },
    )
    return promos


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
    return {
        "rows": rows[:limit] if limit > 0 else rows,
        "total": len(rows),
        "total_qty": total_qty,
    }


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

    # product_id via search_read solo trae [id, display_name], y el
    # display_name de una variante incluye la talla entre paréntesis
    # ("Camiseta algodón negra lisa (XL)"). Ese nombre no existe como tal en
    # product.template.name (la talla nunca es parte del nombre), así que el
    # botón "Ver prendas" del portal nunca encontraba resultados. Resolvemos
    # aquí el nombre "pelado" (campo name, sin variante) para búsqueda/display.
    product_ids = list({l["product_id"][0] for l in lines if l.get("product_id")})
    product_name_map: dict = {}
    if product_ids:
        products = await _call_kw(
            session,
            "product.product",
            "read",
            [product_ids],
            {"fields": ["name"], "context": {"lang": "es_ES"}},
        )
        product_name_map = {p["id"]: p["name"] for p in products}

    alerts = []
    for l in lines:
        remaining = l.get("remaining_quantity") or 0.0
        used = l.get("used_quantity") or 0.0
        total = remaining + used
        if total <= 0 or remaining < 0:
            continue
        ratio = remaining / total
        if ratio < threshold:
            pid = (l.get("product_id") or [None, ""])[0]
            alerts.append(
                {
                    "line_id": l["id"],
                    "product_name": product_name_map.get(pid)
                    or (l.get("product_id") or [None, ""])[1],
                    "size": l.get("product_size_value") or "",
                    "agreement_id": (l.get("sol_agreement_id") or [None, ""])[0],
                    "delegation_name": (l.get("sol_delegation_id") or [None, ""])[1],
                    "dept_name": l.get("dept_name") or "",
                    "remaining": remaining,
                    "total": total,
                    "ratio": ratio,
                }
            )
    alerts.sort(key=lambda a: a["ratio"])
    return {
        "rows": alerts[:limit] if limit > 0 else alerts,
        "total": len(alerts),
    }


# ---------------------------------------------------------------------------
# Acuerdos
# ---------------------------------------------------------------------------


# Campos base + condiciones de pedido/bloqueo de proyecto (portal B2B). Se
# incluyen ya en el listado del dashboard para poder pintar el badge
# "bloqueado"/"cond. activas" y la cuenta atrás sin llamadas extra por tarjeta.
AGREEMENT_FIELDS = [
    "id",
    "name",
    "partner_id",
    "state",
    "date_start",
    "date_end",
    "department_ids",
    "order_blocked",
    "order_blocked_reason",
    "account_manager_id",
    "account_manager_phone",
    "account_manager_email",
    "account_manager_image",
    "order_conditions_active",
    "order_periodicity_days",
    "last_order_request_date",
    "min_garments_per_order",
    "rush_surcharge_pct",
]


@app.get("/api/agreements")
async def list_agreements(session: SessionDep):
    return await _call_kw(
        session,
        "uniform.agreement",
        "search_read",
        [[]],
        {
            "fields": AGREEMENT_FIELDS,
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
            "fields": AGREEMENT_FIELDS,
            "context": {"lang": "es_ES"},
        },
    )
    if not records:
        raise HTTPException(404, "Acuerdo no encontrado")
    return records[0]


@app.get("/api/agreements/{agreement_id}/order_conditions")
async def get_agreement_order_conditions(agreement_id: int, session: SessionDep):
    """Estado de bloqueo/condiciones de pedido para el modal de aviso previo a
    'Nuevo pedido' (contador regresivo, mínimo de prendas, ficha de la
    gestora si el proyecto está bloqueado)."""
    return await _call_kw(
        session, "uniform.agreement", "get_order_gate_info", [[agreement_id]]
    )


@app.put("/api/agreements/{agreement_id}/order_conditions")
async def update_agreement_order_conditions(
    agreement_id: int, request: Request, session: SessionDep
):
    """Configuración de condiciones de pedido y bloqueo — decisión de
    negocio de MY Uniform, solo para el rol Administrador."""
    _require_role(session, "admin")
    body = await request.json()
    allowed_fields = {
        "order_blocked",
        "order_blocked_reason",
        "account_manager_id",
        "order_conditions_active",
        "order_periodicity_days",
        "min_garments_per_order",
        "rush_surcharge_pct",
    }
    vals = {k: v for k, v in body.items() if k in allowed_fields}
    if not vals:
        raise HTTPException(400, "Nada que actualizar")
    await _call_kw(session, "uniform.agreement", "write", [[agreement_id], vals])
    records = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {"fields": AGREEMENT_FIELDS, "context": {"lang": "es_ES"}},
    )
    return records[0] if records else {"ok": True}


CONTACT_SUBJECT_LABELS = {
    "order_request": "Solicitud de pedido",
    "size_query": "Consulta de tallas",
    "order_status": "Estado del pedido",
    "other": "Otro",
}


async def _resolve_manager_partner_ids(session: dict, manager) -> list:
    """manager: tupla [user_id, nombre] de account_manager_id, o falsy.
    Devuelve el partner_id (en lista, para message_post) al que notificar."""
    if not manager:
        return []
    user_recs = await _call_kw(
        session,
        "res.users",
        "read",
        [[manager[0]]],
        {"fields": ["partner_id"], "context": {"lang": "es_ES"}},
    )
    if user_recs and user_recs[0].get("partner_id"):
        return [user_recs[0]["partner_id"][0]]
    return []


def _contact_message_html(subject_label: str, message: str) -> str:
    safe_message = html.escape(message).replace("\n", "<br/>")
    return (
        "<p><strong>Mensaje del cliente desde el portal B2B</strong></p>"
        f"<p><strong>Asunto:</strong> {html.escape(subject_label)}</p>"
        f"<p>{safe_message}</p>"
    )


@app.post("/api/agreements/{agreement_id}/contact_manager")
async def contact_agreement_manager(
    agreement_id: int, request: Request, session: SessionDep
):
    """Formulario 'Escribir a la gestora' del portal (proyecto bloqueado).
    Publica el mensaje en el chatter del acuerdo (mail.thread ya heredado por
    uniform.agreement) y notifica al account_manager_id vía partner_ids, sin
    construir un envío de correo aparte."""
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "El mensaje no puede estar vacío")
    subject_label = CONTACT_SUBJECT_LABELS.get(body.get("subject"), "Otro")

    records = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {"fields": ["name", "account_manager_id"], "context": {"lang": "es_ES"}},
    )
    if not records:
        raise HTTPException(404, "Acuerdo no encontrado")
    partner_ids = await _resolve_manager_partner_ids(
        session, records[0].get("account_manager_id")
    )

    kwargs = {
        "body": _contact_message_html(subject_label, message),
        "body_is_html": True,
        "subtype_xmlid": "mail.mt_comment",
    }
    if partner_ids:
        kwargs["partner_ids"] = partner_ids
    await _call_kw(
        session, "uniform.agreement", "message_post", [[agreement_id]], kwargs
    )
    return {"ok": True}


@app.post("/api/promos/{promo_id}/contact_manager")
async def contact_promo_manager(promo_id: int, request: Request, session: SessionDep):
    """Botón 'Solicitar información' de una promoción del portal.
    A diferencia de contact_agreement_manager, publica el mensaje en el
    chatter de la propia PROMOCIÓN (uniform.portal.promo) — un acuerdo puede
    tener varias promociones para un mismo cliente, así que cada una necesita
    su propio hilo, no el del acuerdo. La gestora a notificar se resuelve a
    través del agreement_id enlazado a la promoción; si la promoción no tiene
    uno propio, se usa el agreement_id que el frontend resolvió como
    respaldo (el mismo que se le muestra al cliente en el wizard — sin esto
    el cliente ve una gestora concreta pero el mensaje no le llegaría a
    nadie)."""
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "El mensaje no puede estar vacío")
    subject_label = CONTACT_SUBJECT_LABELS.get(body.get("subject"), "Otro")

    records = await _call_kw(
        session,
        "uniform.portal.promo",
        "read",
        [[promo_id]],
        {"fields": ["title", "agreement_id"], "context": {"lang": "es_ES"}},
    )
    if not records:
        raise HTTPException(404, "Promoción no encontrada")
    agreement = records[0].get("agreement_id")
    agreement_id = agreement[0] if agreement else body.get("agreement_id")

    partner_ids = []
    if agreement_id:
        agr_records = await _call_kw(
            session,
            "uniform.agreement",
            "read",
            [[agreement_id]],
            {"fields": ["account_manager_id"], "context": {"lang": "es_ES"}},
        )
        if agr_records:
            partner_ids = await _resolve_manager_partner_ids(
                session, agr_records[0].get("account_manager_id")
            )

    kwargs = {
        "body": _contact_message_html(subject_label, message),
        "body_is_html": True,
        "subtype_xmlid": "mail.mt_comment",
    }
    if partner_ids:
        kwargs["partner_ids"] = partner_ids
    await _call_kw(
        session, "uniform.portal.promo", "message_post", [[promo_id]], kwargs
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Look del uniforme (silueta SVG con colores reales por tipo de prenda)
# ---------------------------------------------------------------------------

# La categoría de producto real (product.category, jerarquía "Ropa / superior
# / interior / camisa", "Calzado / seguridad / bota", etc. — ver
# edyma_product_color_group para el equivalente de clasificación de color) ya
# distingue el tipo de prenda; se reutiliza en vez de inventar un campo nuevo.
_GARMENT_SLOT_PREFIXES = [
    ("ropa / inferior", "bottom"),
    ("ropa / mixto", "full_body"),
    ("ropa / superior / interior", "top_inner"),
    ("ropa / superior / exterior", "top_outer"),
    ("ropa / superior / chalecos", "top_outer"),
    ("calzado", "footwear"),
    ("complementos", "accessory"),
]


def _classify_garment_slot(categ_complete_name: str) -> str:
    name = (categ_complete_name or "").strip().lower()
    for prefix, slot in _GARMENT_SLOT_PREFIXES:
        if name.startswith(prefix):
            return slot
    return "other"


async def _build_look_panel(session: dict, domain: list) -> dict:
    """Agrega líneas de pedido (ya acotadas a un acuerdo o departamento) en
    el panel 'Look del uniforme': color dominante (por cantidad pedida) de
    cada tipo de prenda por temporada + listado completo de prendas con su
    código interno."""
    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [domain + [["display_type", "=", False]]],
        {
            "fields": [
                "product_id",
                "product_uom_qty",
                "product_attribute_color_value_id",
                "order_season",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not lines:
        return {"slots": {}, "garments": []}

    product_ids = list({l["product_id"][0] for l in lines if l.get("product_id")})
    color_ids = list(
        {
            l["product_attribute_color_value_id"][0]
            for l in lines
            if l.get("product_attribute_color_value_id")
        }
    )

    prod_categ: dict = {}
    prod_code: dict = {}
    if product_ids:
        prods = await _call_kw(
            session,
            "product.product",
            "read",
            [product_ids],
            {"fields": ["categ_id", "default_code"], "context": {"lang": "es_ES"}},
        )
        for p in prods:
            prod_categ[p["id"]] = p["categ_id"][0] if p.get("categ_id") else None
            prod_code[p["id"]] = p.get("default_code") or ""

    categ_ids = list({c for c in prod_categ.values() if c})
    categ_name: dict = {}
    if categ_ids:
        categs = await _call_kw(
            session,
            "product.category",
            "read",
            [categ_ids],
            {"fields": ["complete_name"], "context": {"lang": "es_ES"}},
        )
        categ_name = {c["id"]: c["complete_name"] for c in categs}

    color_info: dict = {}
    if color_ids:
        colors = await _call_kw(
            session,
            "product.attribute.value",
            "read",
            [color_ids],
            {"fields": ["name", "html_color"], "context": {"lang": "es_ES"}},
        )
        color_info = {c["id"]: c for c in colors}

    slot_qty: dict = {}  # {slot: {season: {color_id: qty}}}
    garments: dict = {}  # {product_id: {...}}
    for l in lines:
        if not l.get("product_id"):
            continue
        pid = l["product_id"][0]
        categ_id = prod_categ.get(pid)
        slot = _classify_garment_slot(categ_name.get(categ_id, ""))
        season = l.get("order_season") or "all"
        color = l.get("product_attribute_color_value_id")
        color_id = color[0] if color else None
        qty = l.get("product_uom_qty") or 0

        by_season = slot_qty.setdefault(slot, {}).setdefault(season, {})
        if color_id:
            by_season[color_id] = by_season.get(color_id, 0) + qty

        g = garments.setdefault(
            pid,
            {
                "product_id": pid,
                "name": l["product_id"][1],
                "code": prod_code.get(pid, ""),
                "slot": slot,
                "color_name": color[1] if color else "",
                "color_hex": (
                    color_info.get(color_id, {}).get("html_color") if color_id else None
                ),
                "qty": 0,
            },
        )
        g["qty"] += qty

    slots_out: dict = {}
    for slot, by_season in slot_qty.items():
        slots_out[slot] = {}
        for season, by_color in by_season.items():
            if not by_color:
                continue
            dominant_id = max(by_color, key=by_color.get)
            c = color_info.get(dominant_id, {})
            slots_out[slot][season] = {
                "color_name": c.get("name", ""),
                "color_hex": c.get("html_color") or "#CCCCCC",
            }

    return {
        "slots": slots_out,
        "garments": sorted(garments.values(), key=lambda g: (g["slot"], g["name"])),
    }


@app.get("/api/agreements/{agreement_id}/look")
async def get_agreement_look(agreement_id: int, session: SessionDep):
    panel = await _build_look_panel(session, [["sol_agreement_id", "=", agreement_id]])
    agreement = await _call_kw(
        session,
        "uniform.agreement",
        "read",
        [[agreement_id]],
        {
            "fields": ["look_photo_summer", "look_photo_winter"],
            "context": {"lang": "es_ES"},
        },
    )
    if agreement:
        panel["photo_summer"] = agreement[0].get("look_photo_summer") or None
        panel["photo_winter"] = agreement[0].get("look_photo_winter") or None
    return panel


@app.get("/api/departments/{dept_id}/look")
async def get_department_look(dept_id: int, session: SessionDep):
    dept = await _call_kw(
        session,
        "uniform.agreement.department",
        "read",
        [[dept_id]],
        {
            "fields": ["uniform_ids", "look_photo_summer", "look_photo_winter"],
            "context": {"lang": "es_ES"},
        },
    )
    if not dept:
        raise HTTPException(404, "Departamento no encontrado")
    order_ids = dept[0].get("uniform_ids") or []
    panel = (
        await _build_look_panel(session, [["order_id", "in", order_ids]])
        if order_ids
        else {"slots": {}, "garments": []}
    )
    panel["photo_summer"] = dept[0].get("look_photo_summer") or None
    panel["photo_winter"] = dept[0].get("look_photo_winter") or None
    return panel


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
                "responsable_id",
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
                "responsable_id",
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
                "responsable_id",
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
            "responsable_id": dept.get("responsable_id"),
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
    _require_role(session, "delegation_manager")
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

    picking_ids = list({m["picking_id"][0] for m in moves if m.get("picking_id")})
    if picking_ids:
        pickings = await _call_kw(
            session,
            "stock.picking",
            "read",
            [picking_ids],
            {
                "fields": [
                    "id",
                    "picking_type_code",
                    "delivery_signature_date",
                    "delivery_signed_by",
                ],
                "context": {"lang": "es_ES"},
            },
        )
        # No se transmite el binario de la firma en el listado (peso/privacidad);
        # solo si está firmado, quién y cuándo. La imagen se pide bajo demanda
        # via GET /api/deliveries/{picking_id}/signature.
        picking_sig_map = {p["id"]: p for p in pickings}
        for m in moves:
            pid = m["picking_id"][0] if m.get("picking_id") else None
            pinfo = picking_sig_map.get(pid, {})
            m["signed"] = bool(pinfo.get("delivery_signature_date"))
            m["signed_by"] = pinfo.get("delivery_signed_by") or ""
            m["signature_date"] = pinfo.get("delivery_signature_date") or ""
            # Una devolución valida un albarán de ENTRADA con el mismo
            # sale_line_id que la entrega original (stock.return.picking
            # conserva el link) — sin distinguir por picking_type_code aquí,
            # se contaba como una entrega más en el historial del trabajador.
            m["move_kind"] = (
                "return" if pinfo.get("picking_type_code") == "incoming" else "delivery"
            )

    moves.sort(key=lambda m: m.get("date") or "", reverse=True)
    return moves


@app.get("/api/deliveries/{picking_id}/signature")
async def get_delivery_signature(picking_id: int, session: SessionDep):
    """Firma digital (PNG base64) capturada al entregar este albarán, para
    mostrarla en el historial de entregas/albaranes."""
    pickings = await _call_kw(
        session,
        "stock.picking",
        "read",
        [[picking_id]],
        {
            "fields": [
                "id",
                "name",
                "delivery_signature",
                "delivery_signature_date",
                "delivery_signed_by",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    if not pickings:
        raise HTTPException(404, "Albarán no encontrado")
    p = pickings[0]
    return {
        "picking": p.get("name"),
        "signature": p.get("delivery_signature") or None,
        "signed_by": p.get("delivery_signed_by") or "",
        "signature_date": p.get("delivery_signature_date") or "",
    }


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
    _require_role(session, "delegation_manager")
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
                "image_128",
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
    _require_role(session, "delegation_manager")
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
    _require_role(session, "delegation_manager")
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


@app.get("/api/workers/{worker_id}/available_categories")
async def list_available_worker_categories(worker_id: int, session: SessionDep):
    """Categorías de producto con talla (atributo type='size') que este
    trabajador todavía no tiene registradas, para el botón '+ Añadir
    categoría' de la pantalla Tallar."""
    attrs = await _call_kw(
        session,
        "product.attribute",
        "search_read",
        [[["type", "=", "size"]]],
        {"fields": ["id"], "context": {"lang": "es_ES"}},
    )
    attr_ids = [a["id"] for a in attrs]
    if not attr_ids:
        return []
    lines = await _call_kw(
        session,
        "product.template.attribute.line",
        "search_read",
        [[["attribute_id", "in", attr_ids]]],
        {"fields": ["product_tmpl_id"], "context": {"lang": "es_ES"}},
    )
    tmpl_ids = list(
        {l["product_tmpl_id"][0] for l in lines if l.get("product_tmpl_id")}
    )
    if not tmpl_ids:
        return []
    tmpls = await _call_kw(
        session,
        "product.template",
        "read",
        [tmpl_ids],
        {"fields": ["categ_id"], "context": {"lang": "es_ES"}},
    )
    categs = {t["categ_id"][0]: t["categ_id"][1] for t in tmpls if t.get("categ_id")}
    existing = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "search_read",
        [[["worker_id", "=", worker_id]]],
        {"fields": ["category_id"], "context": {"lang": "es_ES"}},
    )
    existing_categ_ids = {s["category_id"][0] for s in existing if s.get("category_id")}
    missing = [
        {"id": cid, "name": name}
        for cid, name in categs.items()
        if cid not in existing_categ_ids
    ]
    missing.sort(key=lambda c: c["name"])
    return missing


@app.post("/api/workers/{worker_id}/sizes")
async def add_worker_size(worker_id: int, request: Request, session: SessionDep):
    """Añade una categoría de talla nueva a un trabajador (botón '+ Añadir
    categoría' de la pantalla Tallar). A diferencia del PUT masivo, no
    toca las categorías ya registradas."""
    _require_role(session, "delegation_manager")
    body = await request.json()
    category_id = body.get("category_id")
    size_value_id = body.get("size_value_id")
    if not category_id or not size_value_id:
        raise HTTPException(400, "Falta categoría o talla")
    existing = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "search_read",
        [[["worker_id", "=", worker_id], ["category_id", "=", int(category_id)]]],
        {"fields": ["id"], "context": {"lang": "es_ES"}},
    )
    if existing:
        raise HTTPException(409, "Este trabajador ya tiene talla para esa categoría")
    new_id = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "create",
        [
            {
                "worker_id": worker_id,
                "category_id": int(category_id),
                "size_value_id": int(size_value_id),
            }
        ],
    )
    return {"ok": True, "id": new_id}


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
    _require_role(session, "delegation_manager")
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {
            "fields": ["id", "uniform_agreement_id", "order_line"],
            "context": {"lang": "es_ES"},
        },
    )
    if not orders:
        raise HTTPException(404, "Pedido no encontrado")
    agreement_raw = orders[0].get("uniform_agreement_id")
    agreement_id = (
        agreement_raw[0] if isinstance(agreement_raw, list) else agreement_raw
    )
    if agreement_id:
        gate = await _call_kw(
            session, "uniform.agreement", "get_order_gate_info", [[agreement_id]]
        )
        min_garments = gate.get("min_garments_per_order") or 0
        if gate.get("conditions_active") and min_garments:
            lines = await _call_kw(
                session,
                "sale.order.line",
                "search_read",
                [[["order_id", "=", order_id], ["display_type", "=", False]]],
                {"fields": ["product_uom_qty"], "context": {"lang": "es_ES"}},
            )
            total_qty = sum(l.get("product_uom_qty") or 0 for l in lines)
            if total_qty < min_garments:
                raise HTTPException(
                    409,
                    f"Este proyecto exige un mínimo de {min_garments} prendas por "
                    f"pedido; el pedido actual solo suma {total_qty:.0f}.",
                )
    await _call_kw(session, "sale.order", "action_confirm", [[order_id]])
    return {"ok": True}


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: int, session: SessionDep):
    _require_role(session, "delegation_manager")
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
    # sale.order.picking_ids viaja por el grupo de aprovisionamiento y arrastra
    # cualquier movimiento relacionado (transferencias internas, devoluciones),
    # no solo la entrega al cliente — filtramos a solo albaranes de salida.
    return await _call_kw(
        session,
        "stock.picking",
        "search_read",
        [[["id", "in", picking_ids], ["picking_type_code", "=", "outgoing"]]],
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
                "delivery_signature_date",
                "delivery_signed_by",
            ],
            "order": "scheduled_date desc, id desc",
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
        "search_read",
        [[["id", "in", invoice_ids]]],
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
                "narration",
            ],
            "order": "invoice_date desc, id desc",
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
    agreement_id: int,
    request: Request,
    session: SessionDep,
):
    """Agrupa 2+ prendas del acuerdo para compartir un máximo de unidades por trabajador."""
    _require_role(session, "delegation_manager")
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
    _require_role(session, "delegation_manager")
    await _call_kw(session, "uniform.agreement.product.group", "unlink", [[group_id]])
    return {"ok": True}


@app.post("/api/lines/{line_id}/deliver")
async def deliver_line(line_id: int, request: Request, session: SessionDep):
    """Entrega real: valida el albarán de salida de la línea en Odoo.

    Llama a sale.order.line.action_portal_deliver (edyma_orders_per_worker).
    Body opcional: {"quantity": N, "signature": "<PNG base64>", "signed_by": "..."}
    — por defecto entrega todo lo pendiente. La firma (capturada con canvas
    HTML5 en el portal) se guarda en el albarán de salida generado.
    """
    _require_role(session, "delegation_manager", "warehouse")
    try:
        body = await request.json()
    except Exception:
        body = {}
    qty = body.get("quantity")
    quantity_arg = None if qty in (None, "") else float(qty)
    signature = body.get("signature") or None
    signed_by = (body.get("signed_by") or "").strip() or None
    return await _call_kw(
        session,
        "sale.order.line",
        "action_portal_deliver",
        [[line_id], quantity_arg, signature, signed_by],
    )


@app.post("/api/lines/{line_id}/return")
async def return_line(line_id: int, request: Request, session: SessionDep):
    """Devolución real: crea y valida un albarán de entrada que revierte una
    entrega previa de la línea.

    Llama a sale.order.line.action_portal_return (edyma_orders_per_worker).
    Body opcional: {"quantity": N, "reason": "...", "signature": "<PNG base64>",
    "signed_by": "..."} — por defecto devuelve todo lo entregado. La firma
    (capturada con canvas en el portal) se guarda en el albarán de entrada
    generado, igual que en la entrega.
    """
    _require_role(session, "delegation_manager", "warehouse")
    try:
        body = await request.json()
    except Exception:
        body = {}
    qty = body.get("quantity")
    reason = body.get("reason") or None
    signature = body.get("signature") or None
    signed_by = body.get("signed_by") or None
    quantity_arg = None if qty in (None, "") else float(qty)
    return await _call_kw(
        session,
        "sale.order.line",
        "action_portal_return",
        [[line_id], quantity_arg, reason, signature, signed_by],
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
                "season",
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
async def list_all_workers_global(session: SessionDep, page: int = 1, limit: int = 100):
    """Paginado en servidor: sin esto, un cliente con miles de trabajadores
    los cargaría/renderizaría todos de golpe (mismo problema que ya se
    resolvió en /api/products)."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[]],
        {
            "fields": ["id", "name", "worker_ids", "agreement_ids"],
            "context": {"lang": "es_ES"},
        },
    )
    total = await _call_kw(
        session, "uniform.agreement.worker", "search_count", [[]], {}
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
                "image_128",
            ],
            "limit": limit,
            "offset": (page - 1) * limit,
            "order": "name",
            "context": {"lang": "es_ES"},
        },
    )
    workers = await _enrich_workers_with_stats(session, workers)
    pages = max(1, -(-total // limit))
    return {
        "workers": workers,
        "departments": depts,
        "total": total,
        "page": page,
        "pages": pages,
    }


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
                "delivery_signature_date",
                "delivery_signed_by",
            ],
            "order": "scheduled_date desc, id desc",
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
                "narration",
            ],
            "order": "invoice_date desc, id desc",
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
                "responsable_id",
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
            "responsable_id": dept.get("responsable_id"),
        }
        for did in dept.get("delegation_ids", []):
            if did in deleg_map:
                deleg_map[did]["departments"].append(dept_info)
                deleg_map[did]["worker_count"] += nw
                deleg_map[did]["dept_count"] += 1
                deleg_map[did]["order_count"] += no
    return list(deleg_map.values())


@app.get("/api/agreements/{agreement_id}/all_workers")
async def list_all_workers(
    agreement_id: int, session: SessionDep, page: int = 1, limit: int = 100
):
    """Todos los trabajadores de todos los departamentos del acuerdo, paginado
    en servidor (mismo motivo que /api/all_workers)."""
    depts = await _call_kw(
        session,
        "uniform.agreement.department",
        "search_read",
        [[["agreement_ids", "in", agreement_id]]],
        {"fields": ["id", "name", "worker_ids"], "context": {"lang": "es_ES"}},
    )
    all_worker_ids = sorted({wid for d in depts for wid in d.get("worker_ids", [])})
    total = len(all_worker_ids)
    if not total:
        return {"workers": [], "departments": depts, "total": 0, "page": 1, "pages": 1}
    page_ids = all_worker_ids[(page - 1) * limit : page * limit]
    workers = await _call_kw(
        session,
        "uniform.agreement.worker",
        "read",
        [page_ids],
        {
            "fields": [
                "id",
                "name",
                "code",
                "department_ids",
                "delegation_id",
                "size_ids",
                "employment_state",
                "image_128",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    workers = await _enrich_workers_with_stats(session, workers)
    pages = max(1, -(-total // limit))
    return {
        "workers": workers,
        "departments": depts,
        "total": total,
        "page": page,
        "pages": pages,
    }


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
    # image_1920/512 en product.product cae automáticamente a la plantilla si
    # la variante no tiene foto propia (image_variant_1920) — leyendo el de
    # la plantilla aquí siempre se perdía la foto propia de cada variante.
    variant_img = await _call_kw(
        session,
        "product.product",
        "read",
        [[product_id]],
        {"fields": ["image_512"], "context": {"lang": "es_ES"}},
    )
    if variant_img and variant_img[0].get("image_512"):
        rec["image_512"] = variant_img[0]["image_512"]
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


@app.get("/api/products/{product_id}/variants")
async def get_product_variants(product_id: int, session: SessionDep):
    """Todas las variantes de la misma plantilla que product_id, con su
    miniatura propia (o heredada de la plantilla si aún no tiene foto
    propia) — para el selector de foto por variante en Ficha de Prenda."""
    variant = await _call_kw(
        session,
        "product.product",
        "read",
        [[product_id]],
        {"fields": ["product_tmpl_id"], "context": {"lang": "es_ES"}},
    )
    if not variant:
        return []
    tmpl_raw = variant[0].get("product_tmpl_id")
    tmpl_id = tmpl_raw[0] if isinstance(tmpl_raw, list) else tmpl_raw
    return await _call_kw(
        session,
        "product.product",
        "search_read",
        [[["product_tmpl_id", "=", tmpl_id]]],
        {
            "fields": ["id", "display_name", "image_128"],
            "context": {"lang": "es_ES"},
        },
    )


_SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "3XL", "4XL"]


def _size_sort_key(name: str):
    """Orden natural de tallas: letras en su secuencia habitual, luego
    numéricas (calzado) de menor a mayor, y cualquier otra cosa al final
    por orden alfabético."""
    n = (name or "").strip().upper()
    if n in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(n), "")
    try:
        return (1, float(n.replace(",", ".")), "")
    except ValueError:
        return (2, 0.0, n)


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
        return {"total": 0, "pedidas": 0, "entregadas": 0, "orders": [], "by_size": []}
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
        return {"total": 0, "pedidas": 0, "entregadas": 0, "orders": [], "by_size": []}
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
                "product_size_value",
                "sol_agreement_id",
                "sol_delegation_id",
                "sol_department_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )
    total_ped = sum((l.get("product_uom_qty") or 0) for l in lines)
    total_ent = sum((l.get("qty_delivered") or 0) for l in lines)
    by_size_map: dict = {}
    for l in lines:
        size = l.get("product_size_value") or "Sin talla"
        entry = by_size_map.setdefault(size, {"pedidas": 0.0, "entregadas": 0.0})
        entry["pedidas"] += l.get("product_uom_qty") or 0
        entry["entregadas"] += l.get("qty_delivered") or 0
    by_size = [
        {"size": s, "pedidas": int(v["pedidas"]), "entregadas": int(v["entregadas"])}
        for s, v in sorted(by_size_map.items(), key=lambda kv: _size_sort_key(kv[0]))
    ]
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
        "by_size": by_size,
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
    # Mismo motivo que en get_order_deliveries: picking_ids incluye
    # transferencias internas y devoluciones, no solo entregas de salida.
    return await _call_kw(
        session,
        "stock.picking",
        "search_read",
        [[["id", "in", picking_ids], ["picking_type_code", "=", "outgoing"]]],
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
                "delivery_signature_date",
                "delivery_signed_by",
            ],
            "order": "scheduled_date desc, id desc",
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
        "search_read",
        [[["id", "in", invoice_ids]]],
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
                "narration",
            ],
            "order": "invoice_date desc, id desc",
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


@app.get("/api/internal_users")
async def list_internal_users(session: SessionDep):
    """Usuarios internos de MY Uniform, para asignar la gestora de cuenta en
    las condiciones de pedido (configuración — solo Administrador)."""
    _require_role(session, "admin")
    return await _call_kw(
        session,
        "res.users",
        "search_read",
        [[["share", "=", False], ["active", "=", True]]],
        {"fields": ["id", "name"], "context": {"lang": "es_ES"}, "limit": 200},
    )


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
    _require_role(session, "delegation_manager")
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
    _require_role(session, "delegation_manager")
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
    _require_role(session, "delegation_manager")
    body = await request.json()
    await _call_kw(session, "uniform.agreement.department", "write", [[dept_id], body])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pedidos — creación (dentro de un acuerdo)
# ---------------------------------------------------------------------------


@app.post("/api/agreements/{agreement_id}/orders")
async def create_order(agreement_id: int, request: Request, session: SessionDep):
    """Crea el pedido (sale.order) del proyecto. Antes de crearlo valida el
    bloqueo y la periodicidad configurados en el acuerdo (condiciones de
    pedido). Body opcional para pedido urgente fuera de periodicidad:
    {"rush": true, "signature": "<PNG base64>", "signed_by": "..."}.
    """
    _require_role(session, "delegation_manager")
    try:
        body = await request.json()
    except Exception:
        body = {}
    rush = bool(body.get("rush"))
    signature = body.get("signature") or None
    signed_by = (body.get("signed_by") or "").strip() or None

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

    gate = await _call_kw(
        session, "uniform.agreement", "get_order_gate_info", [[agreement_id]]
    )
    if gate.get("blocked"):
        raise HTTPException(
            403,
            "Los pedidos de este proyecto están bloqueados. Contacta con tu "
            "gestora de cuenta.",
        )
    needs_rush = gate.get("conditions_active") and not gate.get("can_order_now")
    if needs_rush and not rush:
        raise HTTPException(
            409,
            f"Todavía no puedes crear un nuevo pedido: faltan "
            f"{gate.get('days_remaining', 0)} día(s) según la periodicidad "
            f"acordada. Solicita un pedido urgente (+{gate.get('rush_surcharge_pct', 0)}%) si es necesario.",
        )
    if needs_rush and rush:
        if not signature or not signed_by:
            raise HTTPException(
                400,
                "El pedido urgente requiere el nombre y la firma de quien acepta el recargo.",
            )
        surcharge_pct = gate.get("rush_surcharge_pct") or 0.0
        await _call_kw(
            session,
            "uniform.order.surcharge.acceptance",
            "create",
            [
                {
                    "agreement_id": agreement_id,
                    "signed_name": signed_by,
                    "signature": signature,
                    "surcharge_pct": surcharge_pct,
                }
            ],
        )

    fields_vals: dict = {
        "partner_id": partner_id,
        "uniform_agreement_id": agreement_id,
    }
    new_id = await _call_kw(session, "sale.order", "create", [fields_vals])

    if gate.get("conditions_active"):
        await _call_kw(
            session, "uniform.agreement", "register_order_requested", [[agreement_id]]
        )
    if needs_rush and rush:
        acceptances = await _call_kw(
            session,
            "uniform.order.surcharge.acceptance",
            "search_read",
            [[["agreement_id", "=", agreement_id], ["sale_order_id", "=", False]]],
            {"fields": ["id"], "order": "id desc", "limit": 1},
        )
        if acceptances:
            await _call_kw(
                session,
                "uniform.order.surcharge.acceptance",
                "write",
                [[acceptances[0]["id"]], {"sale_order_id": new_id}],
            )

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
    _require_role(session, "delegation_manager")
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


async def _upsert_worker_size(
    session: dict, worker_id: int, category_id: int, size_value_id: int
) -> None:
    """Crea o actualiza la talla del trabajador para una categoría de
    producto. Se llama al guardar una celda de la rejilla de reparto con
    una talla resuelta, para que quede "prerellenada" en la pantalla
    Tallar y en el resto de prendas de la misma categoría, tal y como
    indica el aviso de la vista de pedido."""
    existing = await _call_kw(
        session,
        "uniform.agreement.worker.size",
        "search_read",
        [[["worker_id", "=", worker_id], ["category_id", "=", category_id]]],
        {"fields": ["id", "size_value_id"], "context": {"lang": "es_ES"}},
    )
    if existing:
        current = (
            existing[0]["size_value_id"][0]
            if existing[0].get("size_value_id")
            else None
        )
        if current != size_value_id:
            await _call_kw(
                session,
                "uniform.agreement.worker.size",
                "write",
                [[existing[0]["id"]], {"size_value_id": size_value_id}],
            )
    else:
        await _call_kw(
            session,
            "uniform.agreement.worker.size",
            "create",
            [
                {
                    "worker_id": worker_id,
                    "category_id": category_id,
                    "size_value_id": size_value_id,
                }
            ],
        )


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


@app.get("/api/orders/{order_id}/grid")
async def get_order_grid(order_id: int, session: SessionDep):
    """Datos agregados para la tabla cruzada editable de v-divide: grupos
    con cupo compartido, prendas sueltas del proyecto, trabajadores del
    pedido y del acuerdo, y el estado de cada celda (talla/cantidad/stock)."""
    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {
            "fields": ["id", "name", "state", "uniform_agreement_id"],
            "context": {"lang": "es_ES"},
        },
    )
    if not orders:
        raise HTTPException(404, "Pedido no encontrado")
    order = orders[0]
    agreement_raw = order.get("uniform_agreement_id")
    agreement_id = (
        agreement_raw[0] if isinstance(agreement_raw, list) else agreement_raw
    )

    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["order_id", "=", order_id], ["display_type", "=", False]]],
        {
            "fields": [
                "id",
                "product_id",
                "product_uom_qty",
                "qty_delivered",
                "worker_ids",
            ],
            "context": {"lang": "es_ES"},
        },
    )

    project_lines = lines
    if agreement_id:
        ag_orders = await _call_kw(
            session,
            "sale.order",
            "search_read",
            [[["uniform_agreement_id", "=", agreement_id]]],
            {"fields": ["id"], "context": {"lang": "es_ES"}},
        )
        agreement_order_ids = [o["id"] for o in ag_orders]
        project_lines = await _call_kw(
            session,
            "sale.order.line",
            "search_read",
            [[["order_id", "in", agreement_order_ids], ["display_type", "=", False]]],
            {"fields": ["product_id"], "context": {"lang": "es_ES"}},
        )

    product_ids = list(
        {l["product_id"][0] for l in (lines + project_lines) if l.get("product_id")}
    )
    variants = []
    if product_ids:
        variants = await _call_kw(
            session,
            "product.product",
            "read",
            [product_ids],
            {
                "fields": [
                    "id",
                    "product_tmpl_id",
                    "qty_available",
                    "product_template_attribute_value_ids",
                ],
                "context": {"lang": "es_ES"},
            },
        )
    variant_map = {v["id"]: v for v in variants}

    groups = []
    grouped_tmpl_ids: set = set()
    if agreement_id:
        raw_groups = await _call_kw(
            session,
            "uniform.agreement.product.group",
            "search_read",
            [[["agreement_id", "=", agreement_id]]],
            {
                "fields": ["id", "name", "product_ids", "max_qty"],
                "context": {"lang": "es_ES"},
            },
        )
        group_variant_ids = list({pid for g in raw_groups for pid in g["product_ids"]})
        group_variants = {}
        if group_variant_ids:
            gv = await _call_kw(
                session,
                "product.product",
                "read",
                [group_variant_ids],
                {"fields": ["id", "product_tmpl_id"], "context": {"lang": "es_ES"}},
            )
            group_variants = {v["id"]: v for v in gv}
        for g in raw_groups:
            tmpl_ids_seen: list = []
            products = []
            for pid in g["product_ids"]:
                v = group_variants.get(pid)
                if not v:
                    continue
                tid = v["product_tmpl_id"][0]
                if tid in tmpl_ids_seen:
                    continue
                tmpl_ids_seen.append(tid)
                products.append({"template_id": tid, "name": v["product_tmpl_id"][1]})
            grouped_tmpl_ids.update(tmpl_ids_seen)
            groups.append(
                {
                    "id": g["id"],
                    "name": g["name"],
                    "max_qty": g["max_qty"],
                    "products": products,
                }
            )

    loose_products = []
    seen_tmpl: set = set()
    for l in project_lines:
        pid = l.get("product_id")
        if not pid:
            continue
        v = variant_map.get(pid[0])
        if not v:
            continue
        tid = v["product_tmpl_id"][0]
        if tid in grouped_tmpl_ids or tid in seen_tmpl:
            continue
        seen_tmpl.add(tid)
        loose_products.append({"template_id": tid, "name": v["product_tmpl_id"][1]})

    all_template_ids = list(grouped_tmpl_ids | seen_tmpl)
    size_options_by_tmpl = {
        tid: await _template_size_options(session, tid) for tid in all_template_ids
    }
    size_option_ids_by_tmpl = {
        tid: {o["id"] for o in opts} for tid, opts in size_options_by_tmpl.items()
    }

    # Talla real de cada variante ya vendida (independiente de la tabla de
    # tallas del trabajador): necesaria para que una línea ya guardada con
    # una talla concreta no vuelva a pedir "Tallar" en cuanto se recarga la
    # rejilla, aunque el trabajador no tenga esa categoría registrada.
    variant_size_value: dict = {}
    all_ptav_ids = list(
        {
            pid
            for v in variants
            for pid in (v.get("product_template_attribute_value_ids") or [])
        }
    )
    if all_ptav_ids:
        ptavs = await _call_kw(
            session,
            "product.template.attribute.value",
            "read",
            [all_ptav_ids],
            {"fields": ["product_attribute_value_id"], "context": {"lang": "es_ES"}},
        )
        ptav_to_value = {p["id"]: p["product_attribute_value_id"][0] for p in ptavs}
        for v in variants:
            tid = v["product_tmpl_id"][0]
            size_ids = size_option_ids_by_tmpl.get(tid) or set()
            if not size_ids:
                continue
            for ptav_id in v.get("product_template_attribute_value_ids") or []:
                val_id = ptav_to_value.get(ptav_id)
                if val_id in size_ids:
                    variant_size_value[v["id"]] = val_id
                    break

    categ_by_tmpl: dict = {}
    if all_template_ids:
        tmpls = await _call_kw(
            session,
            "product.template",
            "read",
            [all_template_ids],
            {"fields": ["categ_id"], "context": {"lang": "es_ES"}},
        )
        categ_by_tmpl = {
            t["id"]: (t["categ_id"][0] if t.get("categ_id") else None) for t in tmpls
        }

    worker_ids_in_order = list(
        {wid for l in lines for wid in (l.get("worker_ids") or [])}
    )
    workers_in_order = []
    if worker_ids_in_order:
        workers_in_order = await _call_kw(
            session,
            "uniform.agreement.worker",
            "read",
            [worker_ids_in_order],
            {"fields": ["id", "name"], "context": {"lang": "es_ES"}},
        )

    workers_available = []
    if agreement_id:
        agreement_workers = await list_all_workers(agreement_id, session)
        workers_available = [
            w
            for w in agreement_workers["workers"]
            if w["id"] not in worker_ids_in_order
        ]

    all_worker_ids_for_sizes = worker_ids_in_order + [
        w["id"] for w in workers_available
    ]
    worker_sizes: dict = {}
    if all_worker_ids_for_sizes:
        sizes = await _call_kw(
            session,
            "uniform.agreement.worker.size",
            "search_read",
            [[["worker_id", "in", all_worker_ids_for_sizes]]],
            {
                "fields": ["worker_id", "category_id", "size_value_id"],
                "context": {"lang": "es_ES"},
            },
        )
        for s in sizes:
            wid = s["worker_id"][0]
            cid = (
                s["category_id"][0]
                if isinstance(s.get("category_id"), list)
                else s.get("category_id")
            )
            if not cid:
                continue
            sv = s["size_value_id"][0] if s.get("size_value_id") else None
            worker_sizes.setdefault(wid, {})[cid] = sv

    lines_by_worker_tmpl: dict = {}
    for l in lines:
        pid = l.get("product_id")
        if not pid:
            continue
        v = variant_map.get(pid[0])
        if not v:
            continue
        tid = v["product_tmpl_id"][0]
        wids = l.get("worker_ids") or []
        wid = wids[0] if wids else None
        lines_by_worker_tmpl[(wid, tid)] = l

    cells = []
    for w in workers_in_order:
        for tid in all_template_ids:
            existing = lines_by_worker_tmpl.get((w["id"], tid))
            categ_id = categ_by_tmpl.get(tid)
            size_value = (worker_sizes.get(w["id"]) or {}).get(categ_id)
            if existing:
                pid = existing["product_id"][0]
                v = variant_map.get(pid, {})
                cells.append(
                    {
                        "worker_id": w["id"],
                        "template_id": tid,
                        "line_id": existing["id"],
                        "product_id": pid,
                        "size_value_id": variant_size_value.get(pid, size_value),
                        "qty": existing.get("product_uom_qty") or 0,
                        "qty_delivered": existing.get("qty_delivered") or 0,
                        "qty_available": v.get("qty_available", 0),
                    }
                )
            else:
                cells.append(
                    {
                        "worker_id": w["id"],
                        "template_id": tid,
                        "line_id": None,
                        "product_id": None,
                        "size_value_id": size_value,
                        "qty": 0,
                        "qty_delivered": 0,
                        "qty_available": None,
                    }
                )

    return {
        "order": {
            "id": order["id"],
            "state": order["state"],
            "name": order.get("name"),
            "agreement_id": agreement_id,
        },
        "groups": groups,
        "loose_products": loose_products,
        "size_options": size_options_by_tmpl,
        "categ_by_template": categ_by_tmpl,
        "worker_sizes": worker_sizes,
        "workers_in_order": workers_in_order,
        "workers_available": workers_available,
        "cells": cells,
    }


@app.put("/api/orders/{order_id}/lines/cell")
async def update_grid_cell(order_id: int, request: Request, session: SessionDep):
    """Alta/edición de una celda de la tabla cruzada de v-divide:
    (worker_id, template_id) -> cantidad. Resuelve la variante por talla,
    valida stock disponible y cupo de grupo, y guarda con
    _upsert_order_line (mismo camino que el importador Excel)."""
    _require_role(session, "delegation_manager")
    body = await request.json()
    worker_id = int(body["worker_id"])
    template_id = int(body["template_id"])
    quantity = float(body.get("quantity") or 0)
    if quantity < 0:
        raise HTTPException(400, "La cantidad no puede ser negativa")
    size_value_id = body.get("size_value_id")
    size_value_id = int(size_value_id) if size_value_id else None

    orders = await _call_kw(
        session,
        "sale.order",
        "read",
        [[order_id]],
        {
            "fields": ["id", "state", "uniform_agreement_id"],
            "context": {"lang": "es_ES"},
        },
    )
    if not orders:
        raise HTTPException(404, "Pedido no encontrado")
    if orders[0]["state"] != "draft":
        raise HTTPException(409, "El pedido no está en borrador: no se puede editar")
    agreement_raw = orders[0].get("uniform_agreement_id")
    agreement_id = (
        agreement_raw[0] if isinstance(agreement_raw, list) else agreement_raw
    )

    if not size_value_id:
        raise HTTPException(
            400, "Falta asignar talla al trabajador antes de indicar cantidad"
        )

    existing_line = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [
            [
                ["order_id", "=", order_id],
                ["product_id.product_tmpl_id", "=", template_id],
                ["worker_ids", "in", [worker_id]],
            ]
        ],
        {
            "fields": ["id", "product_id", "worker_ids", "qty_delivered"],
            "context": {"lang": "es_ES"},
        },
    )
    reference_product_id = existing_line[0]["product_id"][0] if existing_line else None

    variant = await _resolve_variant(
        session, template_id, size_value_id, reference_product_id
    )

    if quantity > 0 and variant["qty_available"] < quantity:
        raise HTTPException(
            400,
            f"Sin stock disponible para esa talla (disponible: {variant['qty_available']:.0f})",
        )

    if agreement_id:
        groups = await _call_kw(
            session,
            "uniform.agreement.product.group",
            "search_read",
            [
                [
                    ["agreement_id", "=", agreement_id],
                    ["product_ids.product_tmpl_id", "=", template_id],
                ]
            ],
            {"fields": ["id", "max_qty", "product_ids"], "context": {"lang": "es_ES"}},
        )
        for group in groups:
            group_variants = await _call_kw(
                session,
                "product.product",
                "read",
                [group["product_ids"]],
                {"fields": ["id", "product_tmpl_id"], "context": {"lang": "es_ES"}},
            )
            group_tmpl_ids = list({v["product_tmpl_id"][0] for v in group_variants})
            sibling_lines = await _call_kw(
                session,
                "sale.order.line",
                "search_read",
                [
                    [
                        ["order_id", "=", order_id],
                        ["worker_ids", "in", [worker_id]],
                        ["product_id.product_tmpl_id", "in", group_tmpl_ids],
                    ]
                ],
                {
                    "fields": ["product_id", "product_uom_qty"],
                    "context": {"lang": "es_ES"},
                },
            )
            other_total = sum(
                l["product_uom_qty"]
                for l in sibling_lines
                if not (
                    reference_product_id and l["product_id"][0] == reference_product_id
                )
            )
            if other_total + quantity > group["max_qty"]:
                raise HTTPException(
                    400,
                    f"Supera el cupo compartido del grupo ({other_total + quantity:.0f}/{group['max_qty']})",
                )

    tmpl = await _call_kw(
        session,
        "product.template",
        "read",
        [[template_id]],
        {"fields": ["name", "categ_id"], "context": {"lang": "es_ES"}},
    )
    tmpl_name = tmpl[0]["name"] if tmpl else ""
    tmpl_categ_id = tmpl[0]["categ_id"][0] if tmpl and tmpl[0].get("categ_id") else None
    if tmpl_categ_id:
        await _upsert_worker_size(session, worker_id, tmpl_categ_id, size_value_id)

    if (
        existing_line
        and existing_line[0]["product_id"][0] != variant["product_id"]
        and (existing_line[0].get("worker_ids") or []) == [worker_id]
    ):
        # La talla cambió de verdad (variante resuelta distinta de la línea
        # existente): sustituir la línea vieja en lugar de crear una nueva,
        # para no dejar una línea huérfana con la talla/cantidad antiguas.
        # Solo tocamos la línea vieja directamente si es EXCLUSIVA de este
        # trabajador (worker_ids == [worker_id], igual que _upsert_order_line);
        # si es una línea compartida por varios trabajadores (o vacía), no la
        # tocamos aquí y caemos al _upsert_order_line de abajo, que crea una
        # línea nueva y dedicada para este trabajador sin afectar a los demás.
        old_line_id = existing_line[0]["id"]
        old_qty_delivered = existing_line[0].get("qty_delivered") or 0
        if quantity <= 0 and old_qty_delivered <= 0:
            await _call_kw(session, "sale.order.line", "unlink", [[old_line_id]])
            return {
                "line_id": None,
                "product_id": variant["product_id"],
                "quantity": 0.0,
                "qty_available": variant["qty_available"],
            }
        await _call_kw(
            session,
            "sale.order.line",
            "write",
            [
                [old_line_id],
                {
                    "product_id": variant["product_id"],
                    "product_uom_qty": quantity,
                    "name": tmpl_name,
                },
            ],
        )
        return {
            "line_id": old_line_id,
            "product_id": variant["product_id"],
            "quantity": quantity,
            "qty_available": variant["qty_available"],
        }

    result = await _upsert_order_line(
        session,
        order_id,
        variant["product_id"],
        quantity,
        worker_id,
        tmpl_name,
    )
    return {
        "line_id": result["line_id"],
        "product_id": variant["product_id"],
        "quantity": result["quantity"],
        "qty_available": variant["qty_available"],
    }


@app.post("/api/orders/{order_id}/lines/import")
async def import_order_lines(order_id: int, request: Request, session: SessionDep):
    _require_role(session, "delegation_manager")
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
