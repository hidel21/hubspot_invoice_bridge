from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    hubspot_access_token = fields.Char(
        string="Access token (private app)",
        config_parameter="hubspot_invoice_bridge.access_token",
        help="Token de la private app de HubSpot. No expira: si se filtra, "
        "revóquelo desde HubSpot y genere uno nuevo.",
    )
    hubspot_client_secret = fields.Char(
        string="Client secret",
        config_parameter="hubspot_invoice_bridge.client_secret",
        help="Clave con la que HubSpot firma los webhooks (HMAC-SHA256). "
        "Sin ella no se puede validar la autenticidad de las peticiones.",
    )
    hubspot_webhook_base_url = fields.Char(
        string="URL pública del webhook",
        config_parameter="hubspot_invoice_bridge.webhook_base_url",
        help="Esquema y dominio públicos exactamente como están configurados "
        "en la Target URL de HubSpot, por ejemplo "
        "https://erp.midominio.com. Es necesario porque la firma se "
        "calcula sobre la URL original y detrás de un proxy inverso Odoo "
        "ve otra distinta. Si se deja vacío se usa web.base.url.",
    )
    hubspot_verify_signature = fields.Boolean(
        string="Validar la firma de los webhooks",
        default=True,
        config_parameter="hubspot_invoice_bridge.verify_signature",
        help="Desactívelo únicamente en desarrollo. Con la validación "
        "desactivada cualquiera que conozca la URL puede provocar la "
        "creación de facturas.",
    )

    hubspot_location_property = fields.Char(
        string="Propiedad de localización",
        config_parameter="hubspot_invoice_bridge.location_property",
        help="Nombre interno de la propiedad del negocio que determina con qué "
        "compañía, diario y posición fiscal se factura. Por defecto "
        "'pipeline'.",
    )
    hubspot_vat_property = fields.Char(
        string="Propiedad del NIT",
        config_parameter="hubspot_invoice_bridge.vat_property",
        help="Propiedad de la empresa que contiene la identificación "
        "tributaria. Por defecto 'nit'.",
    )
    hubspot_billing_email_property = fields.Char(
        string="Propiedad del correo de facturación",
        config_parameter="hubspot_invoice_bridge.billing_email_property",
        help="Por defecto 'correo_de_facturacion'.",
    )
    hubspot_writeback_property = fields.Char(
        string="Propiedad para devolver la factura",
        config_parameter="hubspot_invoice_bridge.writeback_property",
        help="Opcional. Si se indica el nombre interno de una propiedad de "
        "texto del negocio, se escribe en ella el enlace a la factura "
        "creada en Odoo. Déjelo vacío para no escribir en HubSpot.",
    )

    hubspot_discount_is_percentage = fields.Boolean(
        string="La propiedad 'discount' es un porcentaje",
        config_parameter="hubspot_invoice_bridge.discount_is_percentage",
        help="En la semántica estándar de HubSpot 'discount' es un importe. "
        "Márquelo si en este portal se usa como porcentaje. No afecta a "
        "'hs_discount_percentage', que siempre se interpreta como "
        "porcentaje.",
    )
    hubspot_sync_contacts = fields.Boolean(
        string="Crear también el contacto principal",
        default=True,
        config_parameter="hubspot_invoice_bridge.sync_contacts",
        help="Crea el contacto principal del negocio como contacto hijo del "
        "cliente, útil para el envío de la factura.",
    )
    hubspot_responsible_user_id = fields.Many2one(
        "res.users",
        string="Responsable de incidencias",
        config_parameter="hubspot_invoice_bridge.responsible_user_id",
        help="Usuario al que se le asigna una actividad cuando un evento no "
        "se puede facturar (por ejemplo, un negocio sin empresa asociada "
        "o con productos sin homologar).",
    )

    hubspot_webhook_url_preview = fields.Char(
        string="Target URL a registrar en HubSpot",
        compute="_compute_hubspot_webhook_url_preview",
    )

    @api.depends("hubspot_webhook_base_url")
    def _compute_hubspot_webhook_url_preview(self):
        default_base = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        )
        for setting in self:
            base = (setting.hubspot_webhook_base_url or default_base or "").rstrip("/")
            setting.hubspot_webhook_url_preview = (
                "%s/hubspot/webhook/deal" % base if base else ""
            )

    def action_hubspot_sync_stages(self):
        self.ensure_one()
        return self.env["hubspot.deal.stage"].action_sync_from_hubspot()
