# Comparativa: Portal B2B — demo (`portal-b2b-demo`) vs. versión final (`portal-b2b`)

**Fecha:** 2026-08-18.
**Contenedores comparados:** `portal-b2b-demo` (puerto **3001**, imagen `portal-b2b-demo-portal-b2b-demo`) vs. `portal-b2b` (puerto **3000**, imagen `portal-b2b`). Ambos apuntan al mismo Odoo (`odoo17_myuniform-odoo-1`, BD `myuniform`, puerto 8071) — mismos datos, distinto código.
**Método:** diff de código real (`main.py`, `static/index.html`) entre las dos carpetas (`C:\DEV\portal-b2b-demo` y `C:\DEV\portal-b2b`) + verificación HTTP en vivo de ambos contenedores (los 3 servicios responden 200/303 tras el arranque de esta sesión).

> **Origen de la divergencia:** ambas carpetas son clones git independientes con el mismo último commit (`7feed34`, 2026-07-30, "Añade selector de foto por variante en Ficha de Prenda"). A partir de ahí cada una acumuló cambios **sin commitear** por separado — `portal-b2b-demo` se congeló como snapshot para enseñar al cliente en algún momento posterior a esa fecha, mientras `portal-b2b` siguió recibiendo desarrollo. No son "propuesta vs. real" (eso ya lo cubre `ANALISIS_PROPUESTA_B2B_vs_DESARROLLO.md`) — son dos puntos en el tiempo del mismo desarrollo real.

---

## 1. Qué tiene la versión final que la demo no tiene

Backend (`main.py`) — 3 endpoints nuevos, 674 líneas cambiadas en total sobre el commit base (vs. 334 en la demo):

| Función nueva | Endpoint | Qué hace |
|---|---|---|
| `list_available_worker_categories` | `GET /api/workers/{id}/available_categories` | Calcula qué categorías de prenda con talla (`product.attribute.type='size'`) **todavía no tiene asignadas** un trabajador, comparando contra `uniform.agreement.worker.size` |
| `add_worker_size` / `_upsert_worker_size` | `POST /api/workers/{id}/sizes` | Crea el registro de talla para una categoría nueva del trabajador (alta, no solo edición de una ya existente) |
| `contact_promo_manager` / `_resolve_manager_partner_ids` / `_contact_message_html` | `POST /api/promos/{id}/contact_manager` | Desde una promoción del panel de bienvenida (v-portal), envía un mensaje real al gestor comercial del cliente (resuelve destinatario + cuerpo HTML) |

Frontend (`static/index.html`) — funcionalidad nueva conectada a lo anterior, más 2 arreglos de UI:

- **"+ Añadir categoría"** en la pantalla Tallar (v-worker-sizes): `renderAddCatControl()` / `openAddWorkerCategory()` / `submitAddWorkerCategory()` — antes solo se podían editar tallas de categorías ya existentes; ahora se puede dar de alta una categoría que el trabajador no tenía.
- **Clic en alerta de stock mínimo → ver prendas**: `openProductsForStockAlert()` — la demo solo mostraba el aviso de texto; en la versión final el botón "Ver prendas" navega al catálogo filtrado por esa prenda/talla/proyecto.
- **`stripHtml()`**: limpia el HTML crudo de `narration` al mostrar la descripción de una factura — en la demo esa descripción podía salir con etiquetas HTML visibles en vez de texto plano.
- **`_deptSeasonPhoto()`**: helper para la foto de "mirada" estacional (verano/invierno) por departamento, con imagen de fallback si no hay foto real subida — antes ese hueco quedaba sin resolver correctamente en algunos casos.

**No hay funcionalidad que esté en la demo y falte en la versión final** (0 endpoints/funciones "solo en demo") — la versión final es un superconjunto estricto de lo que ve el cliente hoy en la demo, no hay regresiones.

---

## 2. Lo que NO ha cambiado (y es la mayoría del código)

Las funcionalidades más grandes que se construyeron en las sesiones de finales de julio — devoluciones (`action_portal_return`), mensajes/promociones del portal de bienvenida (`portal_messages`/`portal_promos`), agrupación de prendas con cupo compartido (`product_groups`), estado laboral del trabajador (`employment_state`), "tallas pendientes"/"alertas de stock" (`pending_sizes`/`stock_alerts`) — **ya estaban presentes en ambas copias por igual**. Esto confirma que la demo se congeló bastante tarde (después de esas sesiones), no en el commit de julio 30. El resto del diff (~300 líneas en cada lado) son ajustes internos/estilo sin cambio de comportamiento visible.

---

## 3. ⚠️ Hallazgo no buscado: ninguna de las dos copias está commiteada

Ni `portal-b2b` ni `portal-b2b-demo` tienen commiteado su estado actual — `git status` muestra `main.py` y `static/index.html` modificados en ambas, y **son distintos entre sí** (no es el mismo diff duplicado). Esto significa:

- El único registro de "qué vio exactamente el cliente en la demo" vive únicamente en el disco de `C:\DEV\portal-b2b-demo`, sin respaldo en git. Si se borra o se sobrescribe esa carpeta, se pierde la trazabilidad de qué build se le enseñó.
- Si en algún momento se copia `portal-b2b` sobre `portal-b2b-demo` (o viceversa) para "sincronizar", se perderá sin aviso el conocimiento de cuál era cuál.

**Recomendación:** commitear el estado actual de `portal-b2b` (674 líneas, es la versión activa de desarrollo) en una rama, y opcionalmente taguear o commitear también el estado de `portal-b2b-demo` en su propio repo como referencia de "lo que vio el cliente el [fecha real de la demo]" antes de seguir tocando ninguna de las dos carpetas.

---

## 4. Antes de la próxima demo al cliente

Si se va a volver a enseñar el portal, tiene sentido **refrescar `portal-b2b-demo` con el código de `portal-b2b`** (los 3 endpoints + 5 mejoras de UI de la §1 arriba) para que el cliente vea ya:
- Que puede añadir tallas de categorías nuevas a un trabajador sin pasar por Odoo directamente.
- Que las alertas de stock mínimo son accionables (llevan a la prenda), no solo un aviso.
- Facturas con descripción limpia (sin HTML crudo).

Ninguno de los 3 puntos requiere `-u` de módulo Odoo (son solo lectura/escritura sobre modelos ya existentes) — bastaría con `docker cp` de `main.py`/`static/index.html` al contenedor `portal-b2b-demo` + reinicio, siguiendo el patrón ya documentado de despliegue de este proyecto (build ignora cambios, hay que copiar los ficheros directamente).
