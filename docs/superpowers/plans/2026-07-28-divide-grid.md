# Tabla Cruzada Editable v-divide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sustituir la tabla de solo lectura de `v-divide` (Portal B2B MyUniform) por una tabla cruzada trabajador×prenda editable celda a celda, con auto-guardado, bloqueo por talla/stock/cupo de grupo, sin tocar el módulo Odoo `edyma_myuniform`.

**Architecture:** Dos endpoints nuevos en `main.py` (`GET .../grid` de lectura agregada, `PUT .../lines/cell` de escritura) más un helper de guardado compartido (`_upsert_order_line`) que también absorbe al importador Excel existente. En el frontend, `loadDivideTable()` se reescribe para consumir `/grid` y pintar `<input>`/`<select>` editables con auto-guardado en `blur`/`change`.

**Tech Stack:** FastAPI (`main.py`), HTML/JS vanilla sin build step (`static/index.html`), Odoo 17 vía JSON-RPC (`_call_kw`). Sin base de datos propia, sin framework de tests (el repo no tiene pytest ni carpeta `tests/`) — la verificación es por ejecución real: `odoo shell` para asunciones de datos, curl/PowerShell para los endpoints nuevos, Playwright para el flujo completo.

## Global Constraints

- Ni `C:\DEV` ni `C:\DEV\portal-b2b` son repositorios git — **no hay pasos de `git commit`** en este plan; cada tarea termina con "marcar la casilla hecha", no con un commit.
- No se toca el módulo Odoo `edyma_myuniform` en ningún paso — no hace falta `-u`/reinicio del contenedor `odoo17_myuniform-odoo-1`. Solo se despliega el contenedor `portal-b2b` (puerto 3000): `docker cp` + `docker restart portal-b2b` para cambios en `main.py`; solo `docker cp` (sin restart) para `static/index.html`, tal como ya se hace en este proyecto.
- Nunca guardar credenciales del Portal en archivos ni en memoria persistente. Cuando haga falta una sesión autenticada (Tarea 5 y Tarea 8), pedírselas a Sergi en el momento y usarlas solo en variables de entorno de esa sesión de shell.
- Respuestas y mensajes de commit/PR no aplican aquí (no hay repo), pero cualquier texto de cara al usuario debe ir en castellano, igual que el resto del Portal.
- Spec de referencia: `docs/specs/2026-07-28-divide-grid-design.md` (mismo repo). Cualquier duda de comportamiento remite ahí.

---

### Task 1: Helper de guardado compartido `_upsert_order_line` + refactor del importador Excel

**Files:**
- Modify: `main.py:2476-2560` (bloque `# Importación masiva de líneas de pedido` + función `import_order_lines`)

**Interfaces:**
- Produces: `async def _upsert_order_line(session: dict, order_id: int, product_id: int, quantity: float, worker_id: int | None = None, name: str | None = None) -> dict` → `{"line_id": int | None, "quantity": float}`. Usado por Task 1 (importador) y Task 4 (endpoint de celda).

- [ ] **Step 1: Añadir el helper `_upsert_order_line` justo antes de `import_order_lines`**

En `main.py`, localizar el bloque (línea 2476):

```python
# ---------------------------------------------------------------------------
# Importación masiva de líneas de pedido (tallas/cantidades por trabajador)
# ---------------------------------------------------------------------------

@app.post("/api/orders/{order_id}/lines/import")
```

Sustituir por (añade el helper antes del endpoint, sin tocar el endpoint todavía):

```python
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
        await _call_kw(
            session,
            "sale.order.line",
            "write",
            [[match["id"]], {"product_uom_qty": quantity}],
        )
        return {"line_id": match["id"], "quantity": quantity}
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


@app.post("/api/orders/{order_id}/lines/import")
```

- [ ] **Step 2: Refactorizar `import_order_lines` para usar el helper**

Localizar dentro de la misma función (línea ~2538 del fichero original):

```python
        fields: dict = {
            "order_id": order_id,
            "product_id": product_id,
            "product_uom_qty": qty,
            "name": prod_name,
        }
        worker_name = str(
            row.get("Trabajador") or row.get("worker") or row.get("Empleado") or ""
        ).strip()
        if worker_name:
            worker_id = worker_map.get(worker_name.lower())
            if worker_id:
                fields["worker_ids"] = [[6, 0, [worker_id]]]
            else:
                errors.append(
                    f"Fila {i}: trabajador '{worker_name}' no encontrado (línea creada sin trabajador)"
                )
        try:
            await _call_kw(session, "sale.order.line", "create", [fields])
            created += 1
        except Exception as exc:
            errors.append(f"Fila {i} ({prod_name}): {exc}")
```

Sustituir por:

```python
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
            await _upsert_order_line(session, order_id, product_id, qty, worker_id, prod_name)
            created += 1
        except Exception as exc:
            errors.append(f"Fila {i} ({prod_name}): {exc}")
```

- [ ] **Step 3: Verificar sintaxis**

Run: `python -m py_compile main.py` (desde `C:\DEV\portal-b2b`)
Expected: sin salida, exit code 0.

- [ ] **Step 4: Marcar tarea hecha** (sin commit — no hay repo git).

---

### Task 2: Helpers de resolución de talla/variante

**Files:**
- Modify: `main.py` (añadir justo después del helper `_upsert_order_line` de la Task 1, antes de `@app.post("/api/orders/{order_id}/lines/import")`)

**Interfaces:**
- Consumes: `_call_kw` (ya existente).
- Produces:
  - `async def _template_size_options(session: dict, product_tmpl_id: int) -> list[dict]` → `[{"id": int, "name": str}, ...]`. Usado por Task 3 (`GET .../grid`).
  - `async def _resolve_variant(session: dict, product_tmpl_id: int, size_value_id: int, reference_product_id: int | None = None) -> dict` → `{"product_id": int, "qty_available": float}`. Usado por Task 4 (`PUT .../lines/cell`).

- [ ] **Step 1: Añadir `_template_size_options`**

Justo después de la función `_upsert_order_line` (antes de `@app.post("/api/orders/{order_id}/lines/import")`), añadir:

```python
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
```

- [ ] **Step 2: Añadir `_resolve_variant`**

Justo después de `_template_size_options`, añadir:

```python
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
                    ref_other & (set(v["product_template_attribute_value_ids"]) - size_ptav_ids)
                ),
            )
            return {"product_id": best["id"], "qty_available": best.get("qty_available", 0)}
    chosen = candidates[0]
    return {"product_id": chosen["id"], "qty_available": chosen.get("qty_available", 0)}
```

- [ ] **Step 3: Verificar sintaxis**

Run: `python -m py_compile main.py`
Expected: sin salida, exit code 0.

- [ ] **Step 4: Marcar tarea hecha.**

---

### Task 3: `GET /api/orders/{order_id}/grid`

**Files:**
- Modify: `main.py` (añadir el endpoint justo después de los helpers de la Task 2, antes de `@app.post("/api/orders/{order_id}/lines/import")`)

**Interfaces:**
- Consumes: `_call_kw`, `_template_size_options` (Task 2), `list_all_workers` (ya existente en `main.py:1547`, devuelve `{"workers": [...], "departments": [...]}`).
- Produces: respuesta JSON con shape:
  ```jsonc
  {
    "order": {"id": int, "state": str, "name": str, "agreement_id": int|null},
    "groups": [{"id": int, "name": str, "max_qty": int, "products": [{"template_id": int, "name": str}]}],
    "loose_products": [{"template_id": int, "name": str}],
    "size_options": {"<template_id>": [{"id": int, "name": str}]},
    "categ_by_template": {"<template_id>": int|null},
    "worker_sizes": {"<worker_id>": {"<categ_id>": int}},
    "workers_in_order": [{"id": int, "name": str}],
    "workers_available": [{"id": int, "name": str}],
    "cells": [{"worker_id": int, "template_id": int, "line_id": int|null, "product_id": int|null,
               "size_value_id": int|null, "qty": float, "qty_delivered": float, "qty_available": float|null}]
  }
  ```
  Usado por el frontend en Task 7.

- [ ] **Step 1: Añadir el endpoint**

Justo antes de `@app.post("/api/orders/{order_id}/lines/import")`, añadir:

```python
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
    agreement_id = agreement_raw[0] if isinstance(agreement_raw, list) else agreement_raw

    lines = await _call_kw(
        session,
        "sale.order.line",
        "search_read",
        [[["order_id", "=", order_id], ["display_type", "=", False]]],
        {
            "fields": ["id", "product_id", "product_uom_qty", "qty_delivered", "worker_ids"],
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
            {"fields": ["id", "product_tmpl_id", "qty_available"], "context": {"lang": "es_ES"}},
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
            {"fields": ["id", "name", "product_ids", "max_qty"], "context": {"lang": "es_ES"}},
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
                {"id": g["id"], "name": g["name"], "max_qty": g["max_qty"], "products": products}
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

    worker_ids_in_order = list({wid for l in lines for wid in (l.get("worker_ids") or [])})
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
            w for w in agreement_workers["workers"] if w["id"] not in worker_ids_in_order
        ]

    all_worker_ids_for_sizes = worker_ids_in_order + [w["id"] for w in workers_available]
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
            cid = s["category_id"][0]
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
                        "size_value_id": size_value,
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
```

- [ ] **Step 2: Verificar sintaxis**

Run: `python -m py_compile main.py`
Expected: sin salida, exit code 0.

- [ ] **Step 3: Marcar tarea hecha.**

---

### Task 4: `PUT /api/orders/{order_id}/lines/cell`

**Files:**
- Modify: `main.py` (añadir el endpoint justo después de `get_order_grid`, Task 3)

**Interfaces:**
- Consumes: `_resolve_variant`, `_upsert_order_line` (Task 1/2).
- Body esperado: `{"worker_id": int, "template_id": int, "quantity": number, "size_value_id": int|null}`.
- Produce: `{"line_id": int|null, "product_id": int, "quantity": float, "qty_available": float}` o error HTTP (400/404/409) con `detail` textual.

- [ ] **Step 1: Añadir el endpoint**

Justo después de `get_order_grid` (Task 3), añadir:

```python
@app.put("/api/orders/{order_id}/lines/cell")
async def update_grid_cell(order_id: int, request: Request, session: SessionDep):
    """Alta/edición de una celda de la tabla cruzada de v-divide:
    (worker_id, template_id) -> cantidad. Resuelve la variante por talla,
    valida stock disponible y cupo de grupo, y guarda con
    _upsert_order_line (mismo camino que el importador Excel)."""
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
        {"fields": ["id", "state", "uniform_agreement_id"], "context": {"lang": "es_ES"}},
    )
    if not orders:
        raise HTTPException(404, "Pedido no encontrado")
    if orders[0]["state"] != "draft":
        raise HTTPException(409, "El pedido no está en borrador: no se puede editar")
    agreement_raw = orders[0].get("uniform_agreement_id")
    agreement_id = agreement_raw[0] if isinstance(agreement_raw, list) else agreement_raw

    if not size_value_id:
        raise HTTPException(400, "Falta asignar talla al trabajador antes de indicar cantidad")

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
        {"fields": ["id", "product_id"], "context": {"lang": "es_ES"}},
    )
    reference_product_id = existing_line[0]["product_id"][0] if existing_line else None

    variant = await _resolve_variant(session, template_id, size_value_id, reference_product_id)

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
        if groups:
            group = groups[0]
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
                {"fields": ["product_id", "product_uom_qty"], "context": {"lang": "es_ES"}},
            )
            other_total = sum(
                l["product_uom_qty"]
                for l in sibling_lines
                if not (reference_product_id and l["product_id"][0] == reference_product_id)
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
        {"fields": ["name"], "context": {"lang": "es_ES"}},
    )
    result = await _upsert_order_line(
        session,
        order_id,
        variant["product_id"],
        quantity,
        worker_id,
        tmpl[0]["name"] if tmpl else "",
    )
    return {
        "line_id": result["line_id"],
        "product_id": variant["product_id"],
        "quantity": result["quantity"],
        "qty_available": variant["qty_available"],
    }
```

- [ ] **Step 2: Verificar sintaxis**

Run: `python -m py_compile main.py`
Expected: sin salida, exit code 0.

- [ ] **Step 3: Marcar tarea hecha.**

---

### Task 5: Desplegar backend y verificar con datos reales

**Files:** ninguno nuevo — solo despliegue y verificación de las Tasks 1-4.

- [ ] **Step 1: Copiar `main.py` al contenedor y reiniciar**

```powershell
docker cp "C:\DEV\portal-b2b\main.py" portal-b2b:/app/main.py
docker restart portal-b2b
```

Esperar ~5s y comprobar que el proceso arrancó:

```powershell
docker logs portal-b2b --tail 20
```

Expected: log de arranque de uvicorn sin traceback.

- [ ] **Step 2: Verificar asunciones de datos reales con `odoo shell`** (sin necesitar login del Portal)

⚠️ Puede tardar hasta 180s en arrancar — no interrumpir antes de ese tiempo.

```powershell
docker exec -i odoo17_myuniform-odoo-1 odoo shell -d myuniform --no-http
```

Dentro del shell, pegar:

```python
groups = env['uniform.agreement.product.group'].search([])
print([(g.id, g.name, g.max_qty, g.product_ids.mapped('display_name')) for g in groups])
tmpl = env['product.template'].search([('attribute_line_ids', '!=', False)], limit=1)
size_attr = tmpl.attribute_line_ids.mapped('attribute_id').filtered(lambda a: a.type == 'size')
print(tmpl.display_name, size_attr.mapped('name'))
variant = env['product.product'].search([('product_tmpl_id', '=', tmpl.id)], limit=1)
print(variant.display_name, variant.qty_available)
exit()
```

Expected: los grupos existentes muestran `max_qty` y las prendas agrupadas (confirma que `uniform.agreement.product.group` tiene datos reales que la Task 3 puede leer); el template de ejemplo muestra un atributo de tipo talla; `qty_available` es un número (confirma que el campo estándar de stock es legible sin cambios en el módulo).

Si no hay ningún grupo (`groups` vacío), anotarlo — significa que la columna de cupo compartido no se verá hasta que Sergi cree al menos un grupo desde el Portal o Odoo; no es un fallo del código.

- [ ] **Step 3: Pedir a Sergi credenciales de un usuario del Portal** (no guardar en archivo ni memoria) y probar los endpoints nuevos end-to-end:

```powershell
$env:PB_USER = "usuario_que_de_sergi"
$env:PB_PASS = "password_que_de_sergi"
$login = Invoke-RestMethod -Uri "http://localhost:3000/api/login" -Method Post -ContentType "application/json" -Body (@{username=$env:PB_USER; password=$env:PB_PASS} | ConvertTo-Json) -SessionVariable pbSession
$order = Invoke-RestMethod -Uri "http://localhost:3000/api/agreements/<AGREEMENT_ID>/orders" -WebSession $pbSession
# tomar un order_id en estado draft de $order
Invoke-RestMethod -Uri "http://localhost:3000/api/orders/<ORDER_ID>/grid" -WebSession $pbSession | ConvertTo-Json -Depth 6
```

Expected: JSON con `order.state == "draft"`, `groups`, `loose_products`, `workers_in_order`/`workers_available`, `cells`. Sustituir `<AGREEMENT_ID>`/`<ORDER_ID>` por valores reales existentes (consultar `/api/agreements` primero si hace falta).

- [ ] **Step 4: Probar `PUT .../lines/cell` con un caso válido y uno inválido**

```powershell
$body = @{worker_id=<WORKER_ID>; template_id=<TEMPLATE_ID>; quantity=1; size_value_id=<SIZE_VALUE_ID>} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:3000/api/orders/<ORDER_ID>/lines/cell" -Method Put -ContentType "application/json" -Body $body -WebSession $pbSession
# repetir la misma llamada con quantity=99999 y comprobar que devuelve 400 "Sin stock disponible..."
```

Expected: primera llamada devuelve `{"line_id":..., "quantity":1, ...}`; segunda llamada falla con 400 y mensaje de stock. Volver a llamar `GET .../grid` y confirmar que la celda `(worker_id, template_id)` refleja `qty: 1` y que NO se duplicó ninguna línea (solo un `line_id` para esa combinación).

- [ ] **Step 5: Marcar tarea hecha.**

---

### Task 6: CSS + estructura HTML del panel izquierdo de v-divide

**Files:**
- Modify: `static/index.html:297` (bloque de estilos, tras `.cell-num.ent`)
- Modify: `static/index.html:1123-1149` (panel `apanel` de la vista `v-divide`)

- [ ] **Step 1: Añadir los estilos que faltan para celdas editables**

Localizar (línea 295-297):

```
.cell-num { font-family:var(--font-m); font-size:13px; text-align:center; padding:6px 8px; color:var(--body); }
.cell-num.est { font-weight:700; color:var(--red); }
.cell-num.ent { color:var(--green); font-weight:600; }
```

Añadir justo después:

```
.asel { font-size: 12px; border: 1px solid var(--border); border-radius: 4px; padding: 4px 7px; color: var(--near-black); background: #fff; outline: none; }
.asel.tailor { color: var(--amber); border-color: #FDE68A; background: #FFFBEB; }
.ainput-sm { width:46px; text-align:center; font-family:var(--font-m); font-size:13px; border:1px solid var(--border); border-radius:4px; padding:3px 4px; outline:none; }
.ainput-sm:focus { border-color:var(--red); box-shadow:0 0 0 2px rgba(2,40,79,.10); }
.ainput-sm:disabled, .asel:disabled { opacity:.35; cursor:not-allowed; }
```

- [ ] **Step 2: Reestructurar el panel izquierdo de `v-divide`**

Localizar (dentro de `<div class="view" id="v-divide">`):

```html
      <div class="alay">
        <!-- Panel izq -->
        <div class="apanel">
          <div class="apanel-t">Importar reparto</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:12px;line-height:1.5;">
            Descarga la plantilla ("Plantilla Excel" arriba), rellena trabajador · prenda · cantidad y súbela aquí para repartir el pedido en bloque.
          </div>
          <div style="margin-top:0;">
            <div class="upload-zone" onclick="importDivideExcel()">
              <div class="upload-icon">📤</div>
              <div class="upload-txt">Importar Excel</div>
              <div class="upload-sub">Plantilla con tallas y cantidades</div>
            </div>
          </div>
        </div>

        <!-- Tabla reparto -->
        <div class="atbl-wrap">
```

Sustituir por:

```html
      <div class="alay">
        <!-- Panel izq -->
        <div class="apanel">
          <div class="apanel-t">Prendas del proyecto</div>
          <div id="divide-products-list"><div style="font-size:12px;color:var(--muted);">Selecciona un pedido para ver sus prendas.</div></div>

          <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border);">
            <div class="apanel-t" style="margin-bottom:10px;">Gestión de trabajadores</div>
            <div id="divide-worker-panel"><div style="font-size:12px;color:var(--muted);">Cargando...</div></div>
          </div>

          <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">
            <div class="apanel-t" style="margin-bottom:6px;">Importar reparto</div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:12px;line-height:1.5;">
              Descarga la plantilla ("Plantilla Excel" arriba), rellena trabajador · prenda · cantidad y súbela aquí para repartir el pedido en bloque.
            </div>
            <div class="upload-zone" onclick="importDivideExcel()">
              <div class="upload-icon">📤</div>
              <div class="upload-txt">Importar Excel</div>
              <div class="upload-sub">Plantilla con tallas y cantidades</div>
            </div>
          </div>
        </div>

        <!-- Tabla reparto -->
        <div class="atbl-wrap">
```

- [ ] **Step 3: Marcar tarea hecha** (verificación visual se hace en la Task 8 con Playwright, junto con el JS de la Task 7 — sin JS todavía los contenedores nuevos quedan con su placeholder estático, lo cual es correcto en este punto intermedio).

---

### Task 7: JS de la tabla cruzada editable

**Files:**
- Modify: `static/index.html:4151-4224` (función `loadDivideTable` completa, a sustituir por la versión nueva + funciones auxiliares)

**Interfaces:**
- Consumes: `GET /api/orders/{id}/grid`, `PUT /api/orders/{id}/lines/cell` (Tasks 3-4); helpers ya existentes `escHtml`, `_currentOrderId`.
- Produces: `loadDivideTable()` (misma firma que antes, sigue siendo llamada por `loadDivide()`, `importDivideExcel()` sin cambios en esos llamadores).

- [ ] **Step 1: Sustituir `loadDivideTable` y añadir las funciones auxiliares**

Localizar el bloque completo actual (líneas 4151-4224):

```javascript
async function loadDivideTable(){
  var wrap=document.getElementById('divide-table-wrap');
  if(!wrap||!_currentOrderId)return;
  wrap.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);">Cargando tabla de reparto…</div>';
  try{
    var r=await fetch('/api/orders/'+_currentOrderId+'/lines');
    if(!r.ok){wrap.innerHTML='<div style="padding:32px;text-align:center;color:#DC2626;">Error cargando líneas.</div>';return;}
    var lines=await r.json();
    if(!lines.length){wrap.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);">Sin líneas de pedido.</div>';return;}
    // Collect unique products (preserving order of appearance)
    var prodMap={};var prodIds=[];
    lines.forEach(function(l){
      if(l.product_id){var pid=l.product_id[0];if(!prodMap[pid]){prodMap[pid]=l.product_id[1];prodIds.push(pid);}}
    });
    // Group lines by worker
    var workerMap={};var workerIds=[];
    lines.forEach(function(l){
      if(!l.worker_id)return;
      var wid=l.worker_id[0];
      if(!workerMap[wid]){workerMap[wid]={name:l.worker_id[1],lines:[]};workerIds.push(wid);}
      workerMap[wid].lines.push(l);
    });
    // Lines without worker
    var noWorker=lines.filter(function(l){return !l.worker_id;});
    // Build table
    var colW=120;
    var thProds=prodIds.map(function(pid){
      return '<th class="pcol th-group" style="min-width:'+colW+'px;text-align:center;">'+escHtml(prodMap[pid])+'</th>';
    }).join('');
    var thSub=prodIds.map(function(){
      return '<th class="th-sub pcol" style="text-align:right;">Ped.</th><th class="th-sub pcol ent" style="text-align:right;">Rec.</th>';
    }).join('');
    // Totals per product
    var totPed={};var totRec={};
    prodIds.forEach(function(pid){totPed[pid]=0;totRec[pid]=0;});
    var bodyRows=workerIds.map(function(wid){
      var w=workerMap[wid];
      var cells=prodIds.map(function(pid){
        var wLines=w.lines.filter(function(l){return l.product_id&&l.product_id[0]===pid;});
        var ped=wLines.reduce(function(s,l){return s+Math.round(l.product_uom_qty||0);},0);
        var rec=wLines.reduce(function(s,l){return s+Math.round(l.qty_delivered||0);},0);
        totPed[pid]+=ped;totRec[pid]+=rec;
        var ok=rec>=ped&&ped>0;
        return '<td style="text-align:right;font-family:var(--font-m);">'+ped+'</td>'
          +'<td class="cell-num ent" style="font-family:var(--font-m);">'+rec+'</td>';
      }).join('');
      return '<tr class="tr-grouped"><td><div style="font-weight:600;color:var(--near-black);font-size:13px;">'+escHtml(w.name)+'</div></td>'+cells+'</tr>';
    }).join('');
    // Totals row
    var totCells=prodIds.map(function(pid){
      var ped=totPed[pid];var rec=totRec[pid];var pct=ped?Math.round(rec/ped*100):0;
      return '<td style="text-align:right;font-family:var(--font-m);font-weight:600;">'+ped+'</td>'
        +'<td class="cell-num ent" style="font-family:var(--font-m);"><div class="mini-bar"><div class="mbar"><div class="mbar-fill" style="width:'+pct+'%"></div></div><span class="mini-txt">'+rec+'/'+ped+'</span></div></td>';
    }).join('');
    var noWRows='';
    if(noWorker.length){
      var noWCells=prodIds.map(function(pid){
        var wl=noWorker.filter(function(l){return l.product_id&&l.product_id[0]===pid;});
        var ped=wl.reduce(function(s,l){return s+Math.round(l.product_uom_qty||0);},0);
        var rec=wl.reduce(function(s,l){return s+Math.round(l.qty_delivered||0);},0);
        return '<td style="text-align:right;font-family:var(--font-m);">'+ped+'</td><td class="cell-num ent">'+rec+'</td>';
      }).join('');
      noWRows='<tr><td><div style="color:var(--muted);font-size:12px;">Sin trabajador</div></td>'+noWCells+'</tr>';
    }
    wrap.innerHTML=
      '<table class="atbl" style="min-width:'+(160+prodIds.length*colW*2)+'px;">'
      +'<thead>'
      +'<tr><th rowspan="2" style="min-width:160px;">Trabajador</th>'+thProds+'</tr>'
      +'<tr>'+thSub+'</tr>'
      +'</thead>'
      +'<tbody>'+bodyRows+noWRows+'<tr class="total"><td>Totales</td>'+totCells+'</tr></tbody>'
      +'</table>';
  }catch(e){wrap.innerHTML='<div style="padding:32px;text-align:center;color:#DC2626;">Error.</div>';console.warn('loadDivideTable',e);}
}
```

Sustituir por:

```javascript
var _gridData=null;
var _gridExtraWorkers=[];

async function loadDivideTable(){
  var wrap=document.getElementById('divide-table-wrap');
  if(!wrap||!_currentOrderId)return;
  wrap.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);">Cargando tabla de reparto…</div>';
  _gridExtraWorkers=[];
  try{
    var r=await fetch('/api/orders/'+_currentOrderId+'/grid');
    if(!r.ok){wrap.innerHTML='<div style="padding:32px;text-align:center;color:#DC2626;">Error cargando la tabla.</div>';return;}
    _gridData=await r.json();
    renderGridProductsPanel();
    renderGridWorkerPanel();
    renderGridTable();
  }catch(e){wrap.innerHTML='<div style="padding:32px;text-align:center;color:#DC2626;">Error.</div>';console.warn('loadDivideTable',e);}
}

function _gridCell(workerId,templateId){
  return (_gridData.cells||[]).find(function(c){return c.worker_id===workerId&&c.template_id===templateId;});
}

function _gridWorkerRows(){
  return (_gridData.workers_in_order||[]).concat(_gridExtraWorkers);
}

function renderGridProductsPanel(){
  var cont=document.getElementById('divide-products-list');
  if(!cont||!_gridData)return;
  var items=(_gridData.groups||[]).map(function(g){
    return '<div class="agi"><div class="agi-name">⛓ '+escHtml(g.name)+'<br><span style="color:#0D9488;font-size:10px;font-weight:600;">Cupo compartido · máx '+g.max_qty+'/trabajador</span></div></div>';
  }).join('')+(_gridData.loose_products||[]).map(function(p){
    return '<div class="agi"><div class="agi-name">'+escHtml(p.name)+'</div></div>';
  }).join('');
  cont.innerHTML=items||'<div style="font-size:12px;color:var(--muted);">Sin prendas configuradas para este proyecto.</div>';
}

function renderGridWorkerPanel(){
  var panel=document.getElementById('divide-worker-panel');
  if(!panel||!_gridData)return;
  var avail=(_gridData.workers_available||[]).filter(function(w){
    return !_gridExtraWorkers.some(function(ew){return ew.id===w.id;});
  });
  if(!avail.length){
    panel.innerHTML='<div style="font-size:12px;color:var(--muted);">No hay más trabajadores del acuerdo para añadir.</div>';
    return;
  }
  panel.innerHTML='<div class="worker-sel-list">'+avail.map(function(w){
    return '<label class="worker-sel-row"><input type="checkbox" value="'+w.id+'" /><div style="flex:1;"><div class="worker-sel-name">'+escHtml(w.name)+'</div></div></label>';
  }).join('')+'</div>'
  +'<button class="btn btn-p btn-sm" style="width:100%;margin-top:8px;justify-content:center;" onclick="addCheckedWorkersToGrid()">+ Añadir a la tabla</button>';
}

function addCheckedWorkersToGrid(){
  var panel=document.getElementById('divide-worker-panel');
  if(!panel)return;
  var checked=Array.prototype.slice.call(panel.querySelectorAll('input[type=checkbox]:checked'));
  var ids=checked.map(function(c){return parseInt(c.value);});
  (_gridData.workers_available||[]).forEach(function(w){
    if(ids.indexOf(w.id)!==-1)_gridExtraWorkers.push(w);
  });
  renderGridWorkerPanel();
  renderGridTable();
}

function _renderProductCells(workerId,templateId,isDraft){
  var cell=_gridCell(workerId,templateId)||{worker_id:workerId,template_id:templateId,qty:0,qty_delivered:0,qty_available:null,size_value_id:null};
  var categId=(_gridData.categ_by_template||{})[templateId];
  var workerSizes=(_gridData.worker_sizes||{})[workerId]||{};
  var sizeValueId=cell.size_value_id||workerSizes[categId]||null;
  var options=(_gridData.size_options||{})[templateId]||[];
  var hasSize=!!sizeValueId;
  var noStock=cell.qty_available!==null&&cell.qty_available!==undefined&&cell.qty_available<=0&&(Number(cell.qty)||0)<=0;
  var disabled=!isDraft||!hasSize||noStock;
  var title=!isDraft?'Pedido no editable (no está en borrador)':(!hasSize?'Tallar al trabajador antes de asignar cantidad':(noStock?'Sin stock disponible para esa talla':''));
  var selHtml='<select class="asel'+(hasSize?'':' tailor')+'" '+(isDraft?'':'disabled')+' onchange="onGridSizeChange(this,'+workerId+','+templateId+')" title="'+escHtml(title)+'">'
    +(hasSize?'':'<option value="">⚠ Tallar</option>')
    +options.map(function(o){return '<option value="'+o.id+'"'+(o.id===sizeValueId?' selected':'')+'>'+escHtml(o.name)+'</option>';}).join('')
    +'</select>';
  var qtyHtml='<input class="ainput-sm" type="number" min="0" value="'+(Number(cell.qty)||0)+'" '+(disabled?'disabled':'')+' title="'+escHtml(title)+'" onblur="onGridQtyChange(this,'+workerId+','+templateId+')" onkeydown="if(event.key===\'Enter\')this.blur();" />';
  return '<td>'+selHtml+'</td><td>'+qtyHtml+'</td><td class="cell-num ent">'+(Number(cell.qty_delivered)||0)+'</td>';
}

function renderGridTable(){
  var wrap=document.getElementById('divide-table-wrap');
  if(!wrap||!_gridData)return;
  var isDraft=_gridData.order.state==='draft';
  var groups=_gridData.groups||[];
  var loose=_gridData.loose_products||[];
  var workers=_gridWorkerRows();
  if(!groups.length&&!loose.length){
    wrap.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);">Sin prendas configuradas para este proyecto.</div>';
    return;
  }
  if(!workers.length){
    wrap.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);">Añade trabajadores desde el panel izquierdo para empezar el reparto.</div>';
    return;
  }
  var thRow1=groups.map(function(g){
    return '<th colspan="'+(1+g.products.length*3)+'" class="pcol th-group" style="border-left:3px solid #0D9488;background:rgba(13,148,136,.1);color:#0D9488;">⛓ '+escHtml(g.name)+' · máx '+g.max_qty+'/trab.</th>';
  }).join('')+loose.map(function(p){
    return '<th colspan="3" class="pcol th-group">'+escHtml(p.name)+'</th>';
  }).join('');
  var thRow2=groups.map(function(g){
    return '<th class="th-sub" style="border-left:3px solid #0D9488;background:rgba(13,148,136,.08);color:#0D9488;">Cupo<br>Grupo</th>'
      +g.products.map(function(p){
        return '<th class="th-sub" style="background:rgba(13,148,136,.08);color:#0D9488;">Talla<br>'+escHtml(p.name)+'</th>'
          +'<th class="th-sub" style="background:rgba(13,148,136,.08);color:#0D9488;">Ped.</th>'
          +'<th class="th-sub" style="background:rgba(13,148,136,.06);color:#0D9488;">Rec.</th>';
      }).join('');
  }).join('')+loose.map(function(){
    return '<th class="th-sub pcol">Talla</th><th class="th-sub pcol">Ped.</th><th class="th-sub pcol">Rec.</th>';
  }).join('');
  var bodyRows=workers.map(function(w){
    var groupCells=groups.map(function(g){
      var groupTotal=g.products.reduce(function(s,p){
        var c=_gridCell(w.id,p.template_id);
        return s+(c?Number(c.qty)||0:0);
      },0);
      return '<td style="border-left:3px solid #0D9488;text-align:center;font-family:var(--font-m);font-size:12px;color:#0D9488;">'+groupTotal+'/'+g.max_qty+'</td>'
        +g.products.map(function(p){return _renderProductCells(w.id,p.template_id,isDraft);}).join('');
    }).join('');
    var looseCells=loose.map(function(p){return _renderProductCells(w.id,p.template_id,isDraft);}).join('');
    return '<tr class="tr-grouped"><td><div style="font-weight:600;color:var(--near-black);font-size:13px;">'+escHtml(w.name)+'</div></td>'+groupCells+looseCells+'</tr>';
  }).join('');
  var totalsRow=groups.map(function(g){
    var pedRec=g.products.map(function(p){
      var ped=0,rec=0;
      workers.forEach(function(w){var c=_gridCell(w.id,p.template_id);if(c){ped+=Number(c.qty)||0;rec+=Number(c.qty_delivered)||0;}});
      return '<td></td><td style="text-align:center;font-weight:600;">'+ped+'</td><td class="cell-num ent">'+rec+'</td>';
    }).join('');
    return '<td style="border-left:3px solid #0D9488;"></td>'+pedRec;
  }).join('')+loose.map(function(p){
    var ped=0,rec=0;
    workers.forEach(function(w){var c=_gridCell(w.id,p.template_id);if(c){ped+=Number(c.qty)||0;rec+=Number(c.qty_delivered)||0;}});
    return '<td></td><td style="text-align:center;font-weight:600;">'+ped+'</td><td class="cell-num ent">'+rec+'</td>';
  }).join('');
  wrap.innerHTML='<table class="atbl"><thead>'
    +'<tr><th rowspan="2" style="min-width:160px;">Trabajador</th>'+thRow1+'</tr>'
    +'<tr>'+thRow2+'</tr>'
    +'</thead><tbody>'+bodyRows+'<tr class="total"><td>Totales</td>'+totalsRow+'</tr></tbody></table>';
}

async function onGridSizeChange(sel,workerId,templateId){
  var td=sel.closest('td');
  var qtyInput=td&&td.nextElementSibling?td.nextElementSibling.querySelector('input'):null;
  var qty=qtyInput?(parseFloat(qtyInput.value)||0):0;
  await saveGridCell(workerId,templateId,qty,parseInt(sel.value)||null);
}

async function onGridQtyChange(inp,workerId,templateId){
  var qty=parseFloat(inp.value);
  if(isNaN(qty)||qty<0){alert('Cantidad no válida.');renderGridTable();return;}
  var td=inp.closest('td');
  var selEl=td&&td.previousElementSibling?td.previousElementSibling.querySelector('select'):null;
  var sizeValueId=selEl?(parseInt(selEl.value)||null):null;
  await saveGridCell(workerId,templateId,qty,sizeValueId);
}

async function saveGridCell(workerId,templateId,quantity,sizeValueId){
  try{
    var r=await fetch('/api/orders/'+_currentOrderId+'/lines/cell',{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({worker_id:workerId,template_id:templateId,quantity:quantity,size_value_id:sizeValueId})
    });
    var result=await r.json();
    if(!r.ok)throw new Error(result.detail||'Error al guardar');
    var cell=_gridCell(workerId,templateId);
    if(!cell){cell={worker_id:workerId,template_id:templateId};_gridData.cells.push(cell);}
    cell.line_id=result.line_id;cell.product_id=result.product_id;
    cell.qty=result.quantity;cell.qty_available=result.qty_available;
    cell.size_value_id=sizeValueId;
    renderGridTable();
  }catch(e){
    alert('Error guardando: '+(e.message||e));
    renderGridTable();
  }
}
```

- [ ] **Step 2: Verificar que no queden referencias rotas**

Run (PowerShell, desde `C:\DEV\portal-b2b`):

```powershell
Select-String -Path static\index.html -Pattern "loadDivideTable\(\)" 
```

Expected: sigue apareciendo en `loadDivide()` (línea ~4046), `importDivideExcel()` (línea ~4600-4601) y `createNewOrder()` indirectamente vía `loadDivide()` — ninguna de esas llamadas cambia de firma, así que no hace falta tocarlas.

- [ ] **Step 3: Marcar tarea hecha** (el HTML se sirve directo desde disco vía `StaticFiles`/`FileResponse` con cabeceras `no-store` ya existentes — no hace falta reiniciar el contenedor para probarlo, solo `docker cp` en la Task 8).

---

### Task 8: Desplegar frontend y verificación completa con Playwright

**Files:** ninguno nuevo — despliegue y verificación end-to-end de las Tasks 6-7 sobre el backend ya desplegado en la Task 5.

- [ ] **Step 1: Copiar el HTML actualizado (sin reiniciar el contenedor)**

```powershell
docker cp "C:\DEV\portal-b2b\static\index.html" portal-b2b:/app/static/index.html
```

- [ ] **Step 2: Pedir a Sergi credenciales de un usuario del Portal** (si no siguen vigentes de la Task 5) para el login por navegador. No guardarlas en ningún archivo ni en memoria — solo usarlas en la sesión de Playwright de este paso.

- [ ] **Step 3: Verificación con Playwright — flujo completo**

Con el navegador MCP de Playwright:
1. Navegar a `http://localhost:3000`, iniciar sesión con las credenciales de Sergi.
2. Ir a un acuerdo → Pedidos → abrir un pedido en estado **Borrador** (`v-divide`).
3. Comprobar que la tabla ya no es de solo lectura: hay `<select>` de talla e `<input>` de cantidad por celda, y aparece el panel "Prendas del proyecto" + "Gestión de trabajadores" en el panel izquierdo.
4. Editar una celda que ya tenga cantidad (subir de N a N+1), pulsar Tab/Enter, y comprobar visualmente el valor guardado (sin recargar la página).
5. Recargar la vista (`loadDivideTable` de nuevo, p.ej. saliendo y reentrando al pedido) y comprobar que el nuevo valor persiste — confirma guardado real, no solo estado local.
6. Marcar una talla en una celda antes vacía (sin talla) y escribir una cantidad — comprobar que se crea una línea nueva (repetir el paso 5 para confirmar persistencia).
7. Si hay algún grupo de prendas con cupo, intentar superar `max_qty` para un mismo trabajador y comprobar que aparece el `alert()` de error y el valor no se guarda.
8. Si alguna variante tiene `qty_available` en 0, comprobar que su input aparece deshabilitado con el tooltip de "Sin stock disponible".
9. Usar el panel "Gestión de trabajadores" para añadir un trabajador nuevo a la tabla y comprobar que aparece una fila nueva editable.
10. Confirmar el pedido (botón "✓ Confirmar pedido") y comprobar que la tabla pasa a mostrarse de solo lectura (inputs/`select` deshabilitados).

Si algo falla, revisar `docker logs portal-b2b --tail 100` antes de descartar el comportamiento como "sobrecarga del servidor" (instrucción explícita de Sergi para esta tarea).

- [ ] **Step 4: Marcar tarea hecha** — con esto la tabla cruzada editable de `v-divide` queda funcional y verificada de extremo a extremo.

---

## Cobertura del spec (autorevisión)

- §2.1 Guardado por celda → Tasks 4, 7 (`saveGridCell`, `onGridQtyChange`/`onGridSizeChange`).
- §2.2 Stock real (`qty_available`) → Task 2 (`_resolve_variant`), Task 4 (chequeo 400), Task 7 (deshabilitado + tooltip).
- §2.3 Agrupación con cupo compartido → Task 3 (`groups` en `/grid`), Task 4 (validación `max_qty`), Task 7 (columna "Cupo Grupo" + colspans).
- §2.4 Fusión Estab./Ped. → reflejado en el propio diseño de `/grid` y `/lines/cell` (una sola `quantity` por celda, sin campo "Estab.").
- §2.5 Solo editable en borrador → Task 4 (409 si no draft), Task 7 (`isDraft`/`disabled`).
- §3.4 Refactor del importador Excel → Task 1.
- §5 Casos borde (cantidad 0 → unlink si no entregado, bajar por debajo de entregado → error de Odoo tal cual, concurrencia → revalidación server-side) → Task 1 (`_upsert_order_line`), Task 4 (revalida en cada PUT).
- §6 Verificación → Task 5 (backend) + Task 8 (Playwright end-to-end).
- §7 Fuera de alcance → no se ha creado ningún endpoint de "añadir prenda al proyecto" ni campo de condiciones de pedido, tal como se acotó.
