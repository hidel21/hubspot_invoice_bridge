{
    "name": "HubSpot → Odoo Invoice Bridge",
    "version": "18.0.5.4.1",
    "summary": "Genera facturas de cliente en borrador desde negocios ganados en HubSpot",
    "description": """
Integración HubSpot → Odoo
==========================

Recibe los webhooks de ``deal.propertyChange`` de HubSpot, detecta las
transiciones a una etapa ganada y genera la factura de cliente
correspondiente **en borrador**.

Características
---------------
* Validación de firma ``X-HubSpot-Signature-v3`` (HMAC-SHA256) y ventana
  anti-replay de 5 minutos.
* Respuesta inmediata al webhook (< 5 s) y procesamiento diferido por cron,
  para no provocar reintentos de HubSpot.
* Idempotencia en dos capas: ``eventId`` único y ``hubspot_deal_id`` único
  sobre ``account.move``.
* Detección de etapa ganada mediante los pipelines reales del portal
  (``metadata.isClosed`` / ``probability``), no comparando contra la cadena
  literal ``closedwon``.
* El cliente de la factura es la **empresa** asociada al negocio; si no hay
  ninguna, el evento queda en error y se notifica al responsable.
* Mapeo de localización: una propiedad del negocio en HubSpot determina la
  compañía, el diario y la posición fiscal con los que se factura.
* **Los negocios con hardware generan una orden de venta, no una factura**: es
  la orden la que mueve el inventario. Una factura directa dejaría el equipo
  entregado y en existencias a la vez.
""",
    "author": "Intelli-Next",
    "website": "https://intelli-next.com",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "depends": ["account", "sale", "utm"],
    "data": [
        "security/hubspot_security.xml",
        "security/ir.model.access.csv",
        "data/utm_source.xml",
        "data/ir_cron.xml",
        "views/hubspot_webhook_event_views.xml",
        "views/hubspot_deal_stage_views.xml",
        "views/hubspot_location_mapping_views.xml",
        "views/account_move_views.xml",
        "views/sale_order_views.xml",
        "views/res_partner_views.xml",
        "views/product_views.xml",
        "views/res_config_settings_views.xml",
        "views/hubspot_menus.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
