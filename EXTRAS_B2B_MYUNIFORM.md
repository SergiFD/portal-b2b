# Extras Portal B2B MyUniform — fuera del alcance contratado

> Registro de trabajo añadido al portal B2B que **no** estaba en la propuesta
> comercial firmada (`PROPUESTA MyU PORTAL B2B.pdf`, 27/07/2026, 10.200 €) ni
> en el mockup original (`B2B_MOCKUP.html`). Sirve para tener trazabilidad de
> qué se ha construido de más, por si hay que hablarlo con el cliente
> (alcance/facturación). Cada tarea nueva se añade al final con su fecha.

---

## Tarea 1 — Límite de altura con scroll + filtro de cantidad/orden en el panel Portal

**Fecha:** 2026-08-03

**Descripción:** Las 4 cajas del panel "Portal MY Uniform" (Mensajes de
clientes, Promociones y novedades, Tallas pendientes de entregar, Alertas de
stock mínimo) no tenían ningún límite de altura: con pocos datos no se notaba,
pero con volumen real (decenas de mensajes, promociones o alertas) la caja
crecería sin límite en vertical en vez de quedarse a un tamaño fijo con scroll
propio — un problema de diseño que no estaba contemplado ni en la propuesta
firmada ni en el mockup original (ambos solo mostraban 2-5 filas de ejemplo
por caja, sin pensar en el caso de "muchos resultados").

Añadido:
- Altura máxima fija + scroll interno en las 4 cajas (`overflow-y:auto`), en
  vez de crecimiento indefinido de la página.
- Un control compartido "Mostrar X" (5/10/20/50/Todos) que afecta a las 4
  cajas a la vez.
- Un control de orden "Más nuevo / Más antiguo primero", aplicado solo a
  Mensajes y Promociones (las únicas 2 con fecha real asociada; Alertas de
  stock mantiene su propio orden por urgencia y Tallas pendientes su orden
  actual).

**Por qué es un extra:** ni la propuesta firmada ni el mockup especifican
ningún comportamiento de paginación, límite de altura, ni controles de
filtrado/orden para estas 4 cajas — se añadió de forma proactiva al detectar
el problema de escalabilidad durante las pruebas de esta sesión, no porque
estuviera pedido en el documento contratado.

**Archivos tocados:** `portal-b2b/static/index.html` (CSS + 2 `<select>`
nuevos + las 4 funciones `loadPortal*`), `portal-b2b/main.py` (parámetro
`sort` en `/api/portal_messages` y `/api/portal_promos`; `limit<=0` = sin
límite en los 4 endpoints del panel Portal).

---

## Tarea 2 — Foto de perfil para trabajadores

**Fecha:** 2026-08-03

**Descripción:** `uniform.agreement.worker` no tenía ningún campo de imagen —
la ficha "Prendas asignadas" de un trabajador siempre mostraba un círculo con
iniciales, nunca una foto real. Añadidos `image_1920`/`image_128` al modelo
Odoo (`edyma_myuniform`), expuestos en el formulario de trabajador y en los
endpoints del portal (`/api/all_workers`, `/api/agreements/{id}/all_workers`,
`/api/workers/{id}/sizes`); el frontend ahora pinta la foto real si existe
(cae a las iniciales si no hay foto, sin romper nada existente). Probado
subiendo una foto de ejemplo a un trabajador real (Ivan Roca) y verificado
que se muestra correctamente en el avatar.

**Por qué es un extra:** ni la propuesta firmada ni el mockup mencionan fotos
de trabajador en ningún sitio — es una capacidad nueva (campo de modelo +
API + UI) que no existía antes, no una corrección de algo ya contratado.

**Archivos tocados:** `odoo17_myuniform/custom_addons/edyma_myuniform/models/uniform_agreement_worker.py`,
`views/uniform_agreement_workers_views.xml`, `portal-b2b/main.py`,
`portal-b2b/static/index.html` (`loadWorkerGarments`).
