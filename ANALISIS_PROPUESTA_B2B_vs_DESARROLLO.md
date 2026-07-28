# Análisis: Propuesta comercial "MY Uniform B2B" vs. desarrollo real

**Fuente:** `PROPUESTA MyU PORTAL B2B.pdf` (Edyma → MyUniform), firmada por María del Carmen Monterrubio Berga el 27/07/2026. Coste: 10.200 €, 4 fases / 8 semanas.
**Comparado contra:** el código real que corre en el contenedor `portal-b2b` (`main.py`, backend FastAPI que actúa de proxy JSON-RPC contra Odoo 17 / BD `myuniform`; `static/index.html`, SPA JS vanilla).
**Fecha del análisis:** 2026-07-28.

> Nota importante: la propuesta describe un **prototipo estático** (`B2B_MOCKUP.html`, un único fichero sin backend) y **propone** construir la versión real integrada como componentes OWL nativos de Odoo. Lo que existe hoy en `portal-b2b` es una aplicación **ya funcional en gran parte**, pero construida con una arquitectura distinta a la propuesta (ver §3). El grado de avance real es, en general, mayor de lo que el documento firmado "promete" para las fases iniciales — pero con vacíos concretos en funcionalidades de negocio que si estaban explícitas en el documento (condiciones de pedido, bloqueo de proyectos, firma digital, devoluciones, roles).

---

## 1. Resumen por vista

| # | Vista | ID | Estado |
|---|---|---|---|
| 0 | Portal / acceso | `v-portal` → `login-screen` | ✅ Implementada |
| 1 | Mis Proyectos / Acuerdos | `v-dash` | ✅ Implementada |
| 2 | Delegaciones | `v-deleg` | ✅ Implementada |
| 3 | Departamentos | `v-depts` | ✅ Implementada |
| 4 | Detalle de Proyecto | `v-proj` | ⚠️ Parcial |
| 5 | Trabajadores | `v-workers` | ⚠️ Parcial |
| 6 | Prendas del Trabajador | `v-worker-garments` | ⚠️ Parcial |
| 7 | Asignar Tallas | `v-worker-sizes` | ✅ Implementada |
| 8 | Trabajadores por Prenda | `v-garment-workers` | ✅ Implementada |
| 9 | Pedidos del Proyecto | `v-orders` | ✅ Implementada |
| 10 | Detalle de Pedido | `v-divide` | ⚠️ Parcial |
| 11 | Todas las Prendas | `v-products` | ✅ Implementada |
| 12 | Ficha de Prenda | `v-garment` | ✅ Implementada |
| 13 | Albaranes | `v-albaran` | ⚠️ Parcial |
| 14 | Facturas | `v-invoices` | ✅ Implementada |
| 15 | Mi Perfil | `v-profile` | ✅ Implementada |

---

## 2. Detalle vista a vista

### v-portal — Portal / acceso
✅ **Implementada.** `doLogin()` → `POST /api/login` (main.py:102) autentica de verdad contra `/web/session/authenticate` de Odoo (BD `myuniform`), sin cuenta de servicio hardcodeada. La propuesta describía un panel de "mensajes de la gestora / promociones / tallas pendientes / stock mínimo" — **eso no existe** (no hay ningún endpoint de mensajería, promociones ni alertas de stock mínimo); lo que hoy hace de "v-portal" es solo la pantalla de login.

### v-dash — Mis Proyectos
✅ **Implementada.** `loadDash()` → `GET /api/agreements` (main.py:209) lee `uniform.agreement` real. Exportar Excel funcional. Lo que la propuesta pedía como "tarjeta bloqueada con contacto a gestora" y "condiciones de pedido con contador regresivo" **no existe** (ver §2 en v-orders/v-divide y §4 transversales).

### v-deleg — Delegaciones
✅ **Implementada**, y de hecho más completa que el prototipo. Creación real de delegación (`POST /api/delegations`, main.py:2012) y vinculación de un departamento existente a otra delegación (many2many real, `PUT /api/departments/{id}`). Corresponde a los dos bugs recientes ya corregidos en esta misma sesión de trabajo (delegación huérfana no aparecía / no se refrescaba al crear).

### v-depts — Departamentos
✅ **Implementada.** CRUD completo contra `uniform.agreement.department`, incluida la asignación many2many a varias delegaciones que pedía la propuesta.

### v-proj — Detalle de Proyecto
⚠️ **Parcial.** El grid de prendas con precios/cantidades reales funciona (`GET /api/agreements/{id}/lines`). **Falta:** la agrupación de 2+ prendas para compartir límite de unidades por trabajador — el aviso de la interfaz lo menciona pero no hay checkbox de selección ni endpoint de agrupación detrás; es un texto sin funcionalidad.

### v-workers — Trabajadores
⚠️ **Parcial**, con más terreno ganado del que reconoce la propuesta:
- Importar/Exportar/Nuevo trabajador: la propuesta decía que **no** estaban en el prototipo → **en la app real SÍ están implementados de verdad** (`POST /api/workers`, `POST /api/import/workers`, export Excel).
- **Falta:** estado laboral real (Trabajando/Baja/Pendiente). `renderWorkers()` pinta siempre "Activo" hardcodeado; no hay ningún campo de Odoo leído para esto.

### v-worker-garments — Prendas del Trabajador
⚠️ **Parcial.** Listado de líneas e historial de entregas reales (`stock.move` en estado `done`).
**Falta por completo:**
- **Firma digital** en la entrega — no existe ningún campo/captura de firma en backend ni frontend (solo un comentario CSS vacío).
- **Panel de devoluciones** (registro `DEV-XXXX`, motivo, firma) — no existe en absoluto.

### v-worker-sizes — Asignar Tallas
✅ **Implementada**, una de las piezas más sólidas del proyecto. Guardado real y automático contra `uniform.agreement.worker.size` en Odoo.

### v-garment-workers — Trabajadores por Prenda
✅ **Implementada** con datos reales de asignación/pedido/recepción por trabajador.

### v-orders — Pedidos del Proyecto
✅ **Implementada.** Confirmar (`sale.order.action_confirm`) y cancelar (`sale.order.action_cancel`) son operaciones reales sobre Odoo — la propuesta decía que "Confirmar pedido" no estaba implementado en el prototipo; **ya lo está**.

### v-divide — Detalle de Pedido
⚠️ **Parcial — es la vista donde más diverge del diseño de la propuesta.**
- Crear/confirmar/eliminar pedido: ✅ reales.
- La **tabla cruzada trabajador × prenda editable en pantalla** que describe la propuesta (con selects de talla, bloqueo automático de inputs sin talla) **no existe tal cual**: hoy `v-divide` muestra una tabla de solo resumen (pedido/recibido por trabajador y producto), y el reparto real de cantidades se hace **importando un Excel** (`POST /api/orders/{id}/lines/import`), no editando celdas en la interfaz.
- No existe el botón "Guardar borrador" — cada línea importada se graba directamente en Odoo, así que el concepto de borrador local de la propuesta no aplica a esta arquitectura, pero tampoco se ha sustituido por nada equivalente en pantalla.

### v-products — Todas las Prendas
✅ **Implementada**, con filtros y export Excel reales contra `product.product`.

### v-garment — Ficha de Prenda
✅ **Implementada.** Estadísticas de uso reales por proyecto.

### v-albaran — Albaranes
⚠️ **Parcial.** Listado real contra `stock.picking`, export Excel funcional.
**Falta:** exportar PDF del albarán (no hay endpoint de report para `stock.picking`, solo existe para facturas) y el historial con firmas (no hay ningún dato de firma asociado, coherente con que la firma digital tampoco existe en v-worker-garments).

### v-invoices — Facturas
✅ **Implementada**, incluida la descarga de PDF real (`account.report_invoice_with_payments`) — mejor que lo que pedía la propuesta como vista de "solo consulta".

### v-profile — Mi Perfil
✅ **Implementada**, y de hecho el cambio de contraseña es **real** (`res.users.change_password` contra Odoo) — la propuesta advertía que en el prototipo esto era pura simulación visual; en la app real ya no lo es.

---

## 3. Funcionalidades de negocio transversales (pedidas explícitamente, no ligadas a una sola vista)

| Funcionalidad | Estado |
|---|---|
| Condiciones de pedido (periodicidad, mínimo de prendas, recargo +5% con firma) | ❌ No implementada |
| Bloqueo de proyectos (fondo rojo, ficha de contacto con la gestora) | ❌ No implementada |
| Roles de usuario (Responsable de Delegación / Administrador MY Uniform / Solo consulta / Usuario de almacén) | ❌ No implementada — sesión única sin distinción de permisos |
| Firma digital de entregas | ❌ No implementada |
| Devoluciones de prendas entregadas | ❌ No implementada |
| Mensajes/promociones/alertas de stock del portal de bienvenida | ❌ No implementada |

---

## 4. Diferencias de arquitectura (propuesta firmada vs. lo construido)

| Aspecto | Propuesta | Real |
|---|---|---|
| Frontend | Componentes OWL nativos dentro del backoffice de Odoo | SPA independiente (`static/index.html`, ~4.400 líneas, JS vanilla) |
| Comunicación | JSON-RPC de Odoo consumido directamente por el navegador, sesión Odoo nativa | FastAPI (`main.py`) como proxy: reautentica cada usuario y reenvía a `/web/dataset/call_kw`, con cookie propia (`portal_token`) mapeada a un diccionario **en memoria del proceso** (no Redis — limitación reconocida en el propio código) |
| Modelos de datos | Nuevos: `my.uniform.project`, `my.uniform.delegation`, `my.uniform.department`, `my.uniform.worker`, `my.uniform.delivery.signature`, `my.uniform.agreement.line` | Reutiliza el módulo Odoo ya existente `edyma_myuniform`: `uniform.agreement`, `uniform.agreement.department`, `uniform.agreement.worker`, `uniform.agreement.worker.size` + modelos estándar (`res.partner`, `sale.order`, `stock.picking`, `account.move`, `product.template/product`) |
| Persistencia de sesión | Implícita en la sesión Odoo | Propia, en memoria — no sobrevive a reinicios ni a múltiples workers |

**Implicación práctica:** el cliente ha firmado una propuesta que describe una integración nativa en Odoo (módulo `my_uniform_portal`, propiedad del cliente, sin dependencias externas). Lo que hoy existe es una aplicación externa (FastAPI + estático) que depende de un contenedor Docker aparte. Si esto no se ha hablado ya con el cliente, es un punto a alinear antes de presentar avances de fase, porque afecta a mantenimiento, hosting (la propuesta menciona explícitamente una partida de "Hosting" a revisar según carga) y a la cláusula de "el módulo es propiedad del cliente una vez abonado".

---

## 5. Resumen ejecutivo

**Más avanzado de lo que reconoce el documento:** la mayoría de vistas de consulta y gestión básica (Delegaciones, Departamentos, Trabajadores, Tallas, Prendas, Facturas, Perfil, Pedidos) ya tienen backend real contra Odoo, superando en varios puntos lo que el prototipo original — y la propia propuesta — daban por no implementado (Importar/Exportar trabajadores, confirmar pedido, cambio de contraseña real).

**Pendiente y coincide con lo firmado (aún no entregado):**
1. Firma digital en entregas (v-worker-garments) + historial de firmas en albaranes.
2. Devoluciones de prendas.
3. Condiciones de pedido (periodicidad / mínimo / recargo +5% con firma).
4. Bloqueo de proyectos con contacto a gestora.
5. Roles de usuario diferenciados.
6. Panel de bienvenida (mensajes, promociones, alertas de stock mínimo, tallas pendientes) — hoy v-portal es solo el login.
7. Tabla cruzada editable en v-divide (hoy el reparto se hace por importación Excel, no en pantalla).
8. Exportar PDF de albarán.
9. Agrupación de prendas con cupo compartido en v-proj.
10. Estado laboral real del trabajador (Activo/Baja) en v-workers.

**A alinear con el cliente:** la arquitectura real (FastAPI externo) no coincide con la propuesta firmada (módulo OWL nativo de Odoo) — ver §4.
