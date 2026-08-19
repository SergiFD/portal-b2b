# Mensajes del portal B2B con tipo/icono/remitente

Fecha: 2026-08-03

## Contexto

El panel "Mensajes" del portal B2B (`v-dash`, `loadPortalMessages()`) muestra
registros de `uniform.portal.message` (modelo en `edyma_myuniform`) como texto
plano con fecha, escritos a mano por la gestora desde el backend de Odoo.

Se pide soportar mensajes de estilo "notificación del sistema" con icono,
título en negrita y pie de firma (fecha · remitente · MY Uniform), ej.:

- 📢 Recordatorio de acuerdo — vencimiento próximo, contactar con la gestora.
- ⚠ Aviso de stock limitado — poco stock de un producto/talla concreto.
- ✅ Pedido confirmado — pedido preparado/en camino.

## Alcance de esta fase

Solo la capacidad de mostrar mensajes con este formato enriquecido + 3
registros de ejemplo reales en la BD local para validar visualmente.

**Fuera de alcance** (fase 2, a diseñar aparte): generación automática real
de estos mensajes disparada por vencimiento de acuerdo, stock físico bajo, o
confirmación/entrega de un pedido. Cada disparador tiene reglas de negocio
propias (umbrales, deduplicación para no repetir el aviso cada día, qué cifra
de stock mirar) que no se han decidido todavía.

## Diseño

### 1. Modelo `uniform.portal.message` (`odoo17_myuniform/custom_addons/edyma_myuniform`)

Nuevos campos, todos opcionales para no romper mensajes existentes:

- `title` (Char) — título del mensaje.
- `msg_type` (Selection: `general` [default], `reminder`, `stock`, `success`).
- `author_name` (Char) — remitente en texto libre (ej. "Clara Fontebona",
  "Gestión de stock", "Logística").

Si `title` está vacío, el mensaje se renderiza exactamente igual que hoy
(compatibilidad con los mensajes ya existentes, que no tienen estos campos).

Vista tree/form (`views/uniform_portal_message_views.xml`) actualizada para
que la gestora pueda rellenar `title`, `msg_type`, `author_name` al escribir
un mensaje manual.

### 2. Backend (`portal-b2b/main.py`)

`/api/portal_messages`: añadir `title`, `msg_type`, `author_name` a los
`fields` del `search_read`. Sin cambios de lógica ni de seguridad (el
`ir.rule` ya existente sigue filtrando por cliente/general).

### 3. Frontend (`portal-b2b/static/index.html`, `loadPortalMessages`)

Si `m.title` está presente: icono según `msg_type` + título en negrita,
cuerpo debajo, pie `fecha · author_name · MY Uniform`. Mapeo tipo→icono/color:

- `reminder` → 📢, borde/fondo azul (reutiliza paleta de `.notice.info`)
- `stock` → ⚠, borde/fondo ámbar (reutiliza paleta de `.notice.warn`)
- `success` → ✅, borde/fondo verde (nueva variante)
- `general` / sin tipo → estilo actual (borde rojo, sin icono/título)

### 4. Datos de ejemplo (BD local `myuniform`)

3 registros creados vía `odoo shell -d myuniform`, con `partner_id` vacío
(mensaje general, visible para cualquier cliente logueado):

1. `msg_type='reminder'`, título "Recordatorio de acuerdo", referencia al
   acuerdo real `AC/26/00009` y gestora real `Clara Fontebona`.
2. `msg_type='stock'`, título "Aviso de stock limitado", producto real
   `Zapato de Seguridad Deportivo Low-Cut (REF-009)`, remitente "Gestión de
   stock".
3. `msg_type='success'`, título "Pedido confirmado", remitente "Logística".

## Testing

Verificación visual en `localhost:3000` (recarga del portal) tras aplicar
los 3 cambios y sembrar los ejemplos. No se añaden tests automatizados
(feature de solo lectura/presentación, sin lógica de negocio nueva).
