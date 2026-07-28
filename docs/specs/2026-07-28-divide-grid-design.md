# Tabla cruzada editable en v-divide — diseño

**Fecha:** 2026-07-28
**Alcance:** Portal B2B MyUniform (`C:\DEV\portal-b2b`), vista `v-divide` (Detalle de Pedido).
**Referencias obligatorias:** `B2B_MOCKUP.html` (sección `id="v-divide"`, líneas ~2110-2409 y JS `updateNoSzInputs` ~3884-3901), `ANALISIS_PROPUESTA_B2B_vs_DESARROLLO.md` (§2 v-divide, §5 punto 7).

## 0. Objetivo

Sustituir/complementar la importación de Excel en `v-divide` por una tabla cruzada **editable celda a celda** (trabajadores × prendas), reutilizando la lógica de guardado ya existente (`sale.order.line` vía `_call_kw`), muy fiel al mockup pero con datos reales de Odoo. Sin inventar campos nuevos en el módulo `edyma_myuniform` (no requiere `-u`/reinicio).

## 1. Decisiones ya tomadas (con el usuario)

1. **Guardado:** auto-guardado por celda en `blur`/Enter — igual patrón que `v-worker-sizes` (Asignar Tallas), que ya guarda en automático. Sin botón "Guardar cambios".
2. **Bloqueo por stock:** se usa `qty_available` real de Odoo (`product.product`) de la variante concreta (talla+color) de cada celda. No se usa `remaining_quantity`/`used_quantity` de `sale.order.line` porque ese campo depende de la relación pedido-máster/pedidos-hijo del asistente nativo "Dividir por trabajadores", que los pedidos del Portal no usan (los pedidos del Portal enlazan `uniform_agreement_id` directo, sin `agreement_order_id`).
3. **Agrupación de prendas con cupo compartido:** SÍ se incluye en esta iteración, usando `uniform.agreement.product.group` (ya existe, solo metadata hoy — `max_qty` nunca se valida en ningún sitio). Debe quedar muy fiel al mockup visualmente.
4. **Columna "Estab." vs "Ped." del mockup:** se **fusionan en una sola cantidad editable** (`sale.order.line.product_uom_qty`). El concepto de "cantidad establecida" distinta de la pedida no existe en Odoo y corresponde a "Condiciones de pedido" de la propuesta comercial (fuera de alcance, no implementado, no se inventa un campo nuevo para esto).
5. **Estado del pedido:** la tabla solo es editable si el pedido está en `draft` (Borrador) — coherente con "la personalización solo se hace en borrador" (memoria `project_myuniform_customization_flow`). Pedidos confirmados se muestran de solo lectura.

## 2. Arquitectura

```
Frontend (static/index.html, v-divide)
  loadDivideTable() [reescrito]
    → GET /api/orders/{order_id}/grid   (lectura agregada, todo en una llamada)
    → pinta tabla editable (grupos + prendas sueltas + panel trabajadores)
  saveGridCell(workerId, productId, qty)
    → PUT /api/orders/{order_id}/lines/cell
    → refresca celda + recalcula total de grupo en el DOM

Backend (main.py)
  _resolve_variant_for_worker(session, product_tmpl_id, worker_id, categ_id)  [nuevo helper]
  _upsert_order_line(session, order_id, product_id, worker_id, qty, ...)     [nuevo helper compartido]
  GET  /api/orders/{order_id}/grid           [nuevo]
  PUT  /api/orders/{order_id}/lines/cell     [nuevo]
  POST /api/orders/{order_id}/lines/import   [refactor: usa _upsert_order_line en vez de create ciego]
```

No se toca el módulo Odoo `edyma_myuniform`. Todo el trabajo nuevo vive en `main.py` + `static/index.html`.

## 3. Backend en detalle

### 3.1 Resolución talla → variante (`_resolve_variant_for_worker`)

Replica (sin duplicar código Python de Odoo, reimplementado como lookup de solo lectura vía RPC) el criterio de `divide_by_workers_wizard._get_variant_from_base_product` (`odoo17_myuniform/custom_addons/edyma_myuniform/wizards/divide_by_workers_wizard.py:72-126`):

1. Buscar en `product.template.attribute_line_ids` el atributo con `attribute_id.type == 'size'` (solo puede haber uno activo globalmente — `product_attribute.py`).
2. Talla del trabajador: `uniform.agreement.worker.size` filtrando `worker_id` + `category_id == template.categ_id` → `size_value_id`. Si no existe, la celda queda bloqueada (`⚠ Tallar`).
3. Color: el de la línea ya existente si la hay; si no, el primer valor de color del template.
4. Buscar `product.product` cuyo `product_template_attribute_value_ids` casen exactamente con (talla, color). Si no hay match → error claro ("No existe variante para esa talla").

### 3.2 `GET /api/orders/{order_id}/grid`

Devuelve todo lo necesario para pintar la tabla en una sola llamada:

```jsonc
{
  "order": {"id": 123, "state": "draft", "name": "S00045-2", "agreement_id": 45},
  "groups": [
    {"id": 7, "name": "Camisa MC + Polo Técnico", "max_qty": 3,
     "products": [{"template_id": 10, "name": "Camisa MC"}, {"template_id": 11, "name": "Polo Técnico"}]}
  ],
  "loose_products": [{"template_id": 12, "name": "Chaqueta Polar"}, ...],
  "workers_in_order": [{"id": 55, "name": "J. García Pérez"}],
  "workers_available": [{"id": 56, "name": "F. Bestard Riera"}],
  "cells": [
    {"worker_id": 55, "template_id": 10, "line_id": 901, "product_id": 501,
     "size_value": "M", "size_options": ["S","M","L"], "has_size": true,
     "qty": 2, "qty_delivered": 0, "qty_available": 8}
  ]
}
```

`cells` es una matriz plana (trabajador × prenda) solo para las combinaciones de `workers_in_order`; las filas añadidas desde `workers_available` no tienen `cells` hasta la primera escritura.

### 3.3 `PUT /api/orders/{order_id}/lines/cell`

Body: `{"worker_id": 55, "template_id": 10, "size_value_id": 33, "quantity": 3}`

Pasos:
1. 404 si el pedido no existe. **409** si `state != 'draft'`.
2. Resolver variante (§3.1) con el `size_value_id` indicado (permite override puntual del select, igual que el mockup).
3. Si la prenda pertenece a un grupo: sumar cantidades de las demás prendas del grupo para ese `worker_id` en este pedido (leyendo líneas existentes) y **rechazar con 400** si `suma > max_qty`. Validación nueva (no existía en ningún sitio antes).
4. Comprobar `qty_available` de la variante resuelta; si `quantity` pedido > disponible, **rechazar con 400** (mensaje "Sin stock disponible para esa talla").
5. `_upsert_order_line(...)`: busca línea existente por (`order_id`, `product_id` resuelto, `worker_id` en `worker_ids`); si existe, `write({"product_uom_qty": quantity})`; si no y `quantity > 0`, `create(...)` con el mismo shape de campos que ya usa el importador Excel (`order_id`, `product_id`, `product_uom_qty`, `name`, `worker_ids=[[6,0,[worker_id]]]`). Si `quantity == 0` y no hay línea previa, no-op.
6. Devuelve la celda actualizada (mismo shape que un elemento de `cells` en el punto 3.2) para que el frontend la pinte sin recargar toda la tabla.

### 3.4 Refactor de `import_order_lines` (línea `main.py:2481`)

Sustituir la llamada directa `_call_kw(session, "sale.order.line", "create", [fields])` (línea 2556) por `_upsert_order_line(...)`, resolviendo `worker_id` como entero (ya lo hace) en vez de `worker_ids=[[6,0,[id]]]` inline. Efecto colateral positivo: reimportar el mismo Excel dos veces deja de duplicar líneas.

## 4. Frontend en detalle

### 4.1 Tabla (`loadDivideTable`, `static/index.html:4151`)

- Cabecera de 2 filas fiel al mockup: grupos con 1ª subcolumna informativa "Cupo grupo" (texto `usado/max_qty`, sin input) + 3 subcolumnas por prenda del grupo (Talla / Cantidad / Rec.); prendas sueltas con 3 subcolumnas (Talla / Cantidad / Rec.).
- Fila por `worker` de `workers_in_order` + filas añadidas manualmente desde el panel. Fila de Totales igual que hoy (client-side).
- Celda de cantidad: `<input type="number" min="0">`. `onblur`/`Enter` → `saveGridCell(...)`. Deshabilitada + estilo atenuado + `title` si: sin talla (`has_size=false`), sin stock (`qty_available<=0`), pedido no en borrador.
- Celda de talla: `<select>` con `size_options`; deshabilitado si pedido no en borrador. Cambiar la talla dispara guardado igual que cambiar la cantidad (usa la cantidad actual de la celda).
- Celda "Rec.": solo lectura, `qty_delivered`.

### 4.2 Panel "Gestión de trabajadores"

- Lista `workers_available` (checkboxes) → botón "+ Añadir a la tabla" inserta filas vacías en el DOM (sin llamada al backend hasta la primera cantidad tecleada).
- Prendas: siempre todas (`groups` + `loose_products`), sin alta manual — no existe "añadir prenda al proyecto" en ningún otro sitio del Portal.

### 4.3 Excel

`importDivideExcel` / `downloadOrderTemplate` (líneas 4566-4607) no se tocan salvo que ahora, tras importar, se recarga la tabla editable nueva (misma llamada `loadDivideTable()` que ya hacen).

## 5. Casos borde

- **Cantidad 0** con línea existente y `qty_delivered==0` → `unlink` de la línea (limpieza). Con `qty_delivered>0` → `write(0)` y dejar que Odoo aplique sus propias reglas si las tiene.
- **Bajar por debajo de lo entregado**: se propaga el error de Odoo tal cual (sin validación propia adicional).
- **Concurrencia**: toda validación (stock, cupo, estado) se repite en el servidor en cada `PUT`, no solo en el cliente.

## 6. Verificación

Con Playwright, en local (`odoo17_myuniform`, pedido de prueba en borrador):
1. Cargar `v-divide`, comprobar que la tabla nueva sustituye a la de solo lectura y las celdas son editables.
2. Editar una celda existente → comprobar en `/api/orders/{id}/lines` que se actualizó la misma línea (no se duplicó).
3. Escribir en una celda vacía (trabajador+prenda sin línea previa) → comprobar que se creó una línea nueva con los campos correctos.
4. Provocar bloqueo por talla no asignada, por stock insuficiente y por cupo de grupo superado → comprobar mensaje y que no se escribió nada en Odoo.
5. Confirmar el pedido y comprobar que la tabla pasa a solo lectura.
6. Revisar logs del contenedor si algo falla; no asumir "sobrecarga del servidor".

## 7. Fuera de alcance (explícitamente)

- "Condiciones de pedido" (periodicidad, mínimo, recargo +5%) — item 3 de la propuesta, no relacionado con esta tabla.
- Añadir prendas nuevas al catálogo del proyecto desde esta pantalla.
- Cambiar el criterio de scoping de trabajadores por departamento en el propio `sale.order` (los pedidos del Portal no fijan `uniform_department_id`/`delegation_id`; se mantiene el criterio ya usado en el resto del Portal de listar trabajadores por acuerdo completo).
