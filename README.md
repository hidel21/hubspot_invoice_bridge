# HubSpot → Odoo 18: facturación de negocios ganados

Genera una factura de cliente **en borrador** en Odoo cuando un negocio de
HubSpot entra en una etapa de cierre ganado.

## Cómo funciona

```
HubSpot ──POST /hubspot/webhook/deal──> Controller (auth público, csrf off)
                                            │ 1. valida HMAC-SHA256 v3
                                            │ 2. rechaza si tiene > 5 min
                                            │ 3. encola (eventId único)
                                            └─> responde 200 en ~50 ms
                                                        │
                                            ir.cron (cada 2 minutos)
                                                        │ lee negocio, empresa,
                                                        │ contacto y line items
                                                        └─> account.move (borrador)
```

El webhook **no** crea la factura: HubSpot exige respuesta en unos 5 segundos y
reintenta durante 24 h si no la recibe. Medido en este entorno, el endpoint
responde en 37–51 ms, unas 100 veces por debajo del límite.

## Decisiones de diseño

**El cliente es la empresa, nunca el contacto.** Se toma la empresa asociada al
negocio (la marcada como primaria si hay varias). Si el negocio no tiene
ninguna, el evento queda en estado *error* con el motivo y se crea una actividad
para el usuario responsable configurado; no se factura a medias.

**Siempre en borrador.** El módulo nunca valida ni contabiliza la factura. Falta
completar datos que se revisan a mano (fecha, referencias fiscales, datos del
cliente recién creado).

**La etapa ganada se detecta por pipeline, no por la cadena `closedwon`.** Cada
pipeline tiene su propio identificador de etapa ganada. En este portal:

| Pipeline | Interno | Etapa ganada |
|---|---|---|
| Pipeline de ventas | `default` | `closedwon` |
| Pipeline de ventas - JoobPay | `793842865` | `1163347550` |
| Pipeline Clientes No Gestionados | `666208615` | *(sin etapa de cierre)* |

Comparar contra `closedwon` habría ignorado en silencio todos los negocios de
JoobPay. Las etapas se sincronizan desde la API (`/crm/v3/pipelines/deals`) y la
marca *Genera factura* queda editable a mano.

**Idempotencia en dos capas.** `eventId` único en la cola (absorbe los reintentos
y los lotes repetidos de HubSpot) y `hubspot_deal_id` único en `account.move`
(garantiza una sola factura por negocio, incluso si el negocio entra y sale de la
etapa ganada varias veces).

## Puesta en marcha

### 1. Instalar

El módulo necesita `account`. Añada `custom_addons` al `addons_path` (ya hecho en
`/opt/18.0e/odoo.conf`) e instale desde Aplicaciones o con:

```bash
./venv/bin/odoo -c odoo.conf -d <bd> -i hubspot_invoice_bridge --stop-after-init
```

### 2. Credenciales

**Ajustes → Contabilidad → Integración con HubSpot.** Se necesitan el *access
token* y el *client secret* de la private app. Ambos se guardan en
`ir.config_parameter`; no deben quedar en ficheros del repositorio.

### 3. Endpoint público

HubSpot exige HTTPS accesible desde internet. Odoo escucha en `localhost:8070`,
así que hace falta un proxy inverso con certificado.

En **URL pública del webhook** ponga el esquema y dominio *exactamente* como
figuran en la Target URL de HubSpot (por ejemplo `https://erp.midominio.com`).
Es imprescindible: la firma se calcula sobre la URL original, y detrás de un
proxy Odoo ve `http://localhost:8070/...`, con lo que la validación fallaría
siempre.

La Target URL a registrar en HubSpot se muestra ya construida en la misma
pantalla de ajustes: `<url pública>/hubspot/webhook/deal`.

### 4. Etapas

Pulse **Sincronizar etapas desde HubSpot** y revise en
*Contabilidad → Configuración → HubSpot → Etapas de los pipelines* qué etapas
tienen marcado *Genera factura*.

### 5. Localizaciones de facturación

En *Contabilidad → Configuración → HubSpot → Localizaciones de facturación*
defina, por cada valor de HubSpot, la compañía, el diario de venta y la posición
fiscal. **Debe existir una línea marcada como predeterminada.**

Por defecto se enruta por la propiedad `pipeline` del negocio. Cuando exista la
propiedad de localización dedicada en HubSpot, basta con escribir su nombre
interno en Ajustes → *Propiedad de localización*; no hay que tocar código.

### 6. Homologación de productos

El módulo **no crea productos**. Busca, en este orden:

1. `hubspot_product_id` ya vinculado en el producto de Odoo,
2. `hs_sku` del ítem de línea contra la referencia interna (`default_code`) o el
   código de barras,
3. nombre exacto.

Cuando encuentra por SKU o nombre, guarda el `hs_product_id` para que la
siguiente vez sea una búsqueda directa. Si **algún** producto del negocio no se
puede homologar, no se crea una factura parcial: el evento queda en error
listando los productos afectados.

## Propiedades de HubSpot que se leen

Tomadas del inventario del portal (`resources/Propiedades de HubSpot .xlsx`).
Las marcadas con ★ son personalizadas de este portal.

| Objeto | Propiedad | Uso en Odoo |
|---|---|---|
| Negocio | `dealname` | `ref`, `invoice_origin` |
| Negocio | `dealstage` | disparador de facturación |
| Negocio | `pipeline` | localización (por defecto) |
| Negocio | `deal_currency_code` | `currency_id` (COP / USD / EUR) |
| Negocio | `closedate` | nota interna |
| Negocio | ★ `cotizaciones_aprobadas` | nota interna |
| Negocio | ★ `categoria_de_negocio` | nota interna |
| Negocio | ★ `licencia_elastica`, ★ `modalidad_licencia_anual` | nota interna |
| Empresa | `name`, `address`, `city`, `state`, `zip`, `country`, `phone`, `domain` | datos del cliente |
| Empresa | ★ `nit` | `vat` |
| Empresa | ★ `correo_de_facturacion` | `email` |
| Contacto | `firstname`, `lastname`, `email`, `phone`, `jobtitle` | contacto hijo |
| Ítem de línea | `name`, `hs_sku`, `hs_product_id` | homologación del producto |
| Ítem de línea | `price`, `quantity` | `price_unit`, `quantity` |
| Ítem de línea | `hs_discount_percentage` / `discount` | `discount` |

### Sobre el descuento

HubSpot expone dos propiedades y el inventario del portal deja la duda abierta:

- `hs_discount_percentage` — porcentaje. Se usa tal cual, siempre que venga.
- `discount` — en la semántica estándar de HubSpot es un **importe**, pero el
  inventario lo documenta como porcentaje. La interpretación se controla con la
  casilla *La propiedad `discount` es un porcentaje* en Ajustes. Por defecto se
  trata como importe y se convierte a porcentaje.

**Conviene confirmar cuál de las dos usa realmente el portal** antes de pasar a
producción, porque un descuento mal interpretado altera el importe facturado.

## Impuestos

Los impuestos **no** se copian de HubSpot: los calcula Odoo a partir del
producto y la posición fiscal, que es la única forma de que la factura sea
coherente con la localización fiscal instalada. Verifíquelos en las primeras
facturas.

## Operación y diagnóstico

*Contabilidad → Configuración → HubSpot → Eventos recibidos* muestra la cola con
el payload íntegro de cada evento, los intentos y el error si lo hubo. Estados:

- **Pendiente** — encolado, esperando el cron.
- **Descartado** — no procedía facturar (otra propiedad, etapa no ganada).
- **Procesado** — factura creada (o ya existía).
- **Error** — ver el detalle. Se reintenta hasta 5 veces; luego hay que usar
  *Reintentar*.

## Seguridad

- La firma HMAC se valida siempre salvo que se desactive expresamente en
  Ajustes. **No lo desactive en producción**: sin validación, cualquiera que
  conozca la URL puede provocar la creación de facturas.
- El *client secret* es la clave de esa firma. Si se filtra, se pueden falsificar
  webhooks. Revóquelo y regenérelo en HubSpot si ha estado expuesto.
- El *access token* de una private app **no expira**.

## Verificación realizada

Instalación limpia sobre BD nueva (`hubspot_test`), sin errores ni warnings, y
33 comprobaciones automáticas:

- ingesta, deduplicación por `eventId` y lotes mixtos;
- detección de etapa ganada en los tres pipelines del portal;
- descarte de cambios de otras propiedades y de cierre perdido;
- las cuatro variantes del descuento, con límites y división por cero;
- firma válida, firma inválida, cuerpo manipulado, sin firma, petición caducada,
  lote múltiple y cuerpo ilegible;
- resolución de localización y unicidad de la predeterminada;
- restricción de una sola factura por negocio;
- latencia del endpoint: 37–51 ms.
