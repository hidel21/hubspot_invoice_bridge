from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    hubspot_trigger_mode = fields.Selection(
        selection=[
            ("stage", "El negocio llega a una etapa ganada"),
            ("flag", "Se marca «Cotización Aprobada» en el negocio"),
            ("quote", "Se firma una cotización del negocio"),
            ("stage_and_quote", "Etapa ganada Y cotización firmada"),
            ("stage_or_flag", "Etapa ganada O «Cotización Aprobada»"),
        ],
        string="Qué dispara el documento",
        default="stage",
        config_parameter="hubspot_invoice_bridge.trigger_mode",
        help="Lo habitual es «etapa ganada»: HubSpot valida por dentro la "
        "firma y las cotizaciones aprobadas antes de mover el negocio a "
        "ganado, así que cuando llega aquí ya viene validado.\n\n"
        "Los otros modos existen para portales donde esa validación no ocurre "
        "antes. Cuando se exigen dos condiciones el orden da igual: al llegar "
        "el segundo aviso se consulta el estado actual del negocio.",
    )
    hubspot_deal_flag_property = fields.Char(
        string="Propiedad de cotizaciones aprobadas",
        config_parameter="hubspot_invoice_bridge.deal_flag_property",
        default="cotizaciones_aprobadas",
        help="Nombre interno de la propiedad del negocio que dice CUÁLES son "
        "las cotizaciones aprobadas. No es un sí o un no: lleva una o varias "
        "separadas por coma, por número, título o identificador.\n\n"
        "De ahí salen los productos: si hay dos cotizaciones, el documento de "
        "Odoo trae los artículos y las cantidades de las dos.",
    )
    hubspot_document_mode = fields.Selection(
        selection=[
            ("auto", "Según lo que lleve: orden de venta si hay almacén"),
            ("sale_order", "Siempre orden de venta"),
            ("invoice", "Siempre factura"),
        ],
        string="Qué se crea en Odoo",
        default="sale_order",
        config_parameter="hubspot_invoice_bridge.document_mode",
        help="Una factura no mueve inventario. Si el negocio lleva equipos y "
        "se factura directo, el cliente se queda con un aparato que para Odoo "
        "sigue en la bodega: sin salida, sin serial y sin rastro para la "
        "garantía.\n\n"
        "• Según lo que lleve: si alguna línea es un producto almacenable se "
        "crea una orden de venta, que al confirmarse genera la entrega. Si "
        "todo es licenciamiento o servicio, va directo a factura.\n"
        "• Siempre orden de venta: todo pasa por Ventas, incluso el software.\n"
        "• Siempre factura: el comportamiento anterior. El almacén no se "
        "entera de nada.",
    )

    hubspot_line_items_source = fields.Selection(
        selection=[
            ("auto", "La cotización si la hay, y si no el negocio"),
            ("deal", "Siempre del negocio"),
            ("quote", "Siempre de la cotización"),
        ],
        string="De dónde salen las líneas",
        default="auto",
        config_parameter="hubspot_invoice_bridge.line_items_source",
        help="En HubSpot un producto puede colgar del negocio o de la "
        "cotización. Si el equipo comercial trabaja con cotizaciones, los "
        "productos están ahí y el negocio no tiene ninguno.",
    )
    hubspot_signed_quote_statuses = fields.Char(
        string="Estados que cuentan como firmada",
        config_parameter="hubspot_invoice_bridge.signed_quote_statuses",
        help="Separados por comas. Solo hace falta si vuestro proceso marca la "
        "firma con un estado propio: la firma a mano y la electrónica "
        "completa ya se detectan sin configurar nada.",
    )
    hubspot_ignored_change_sources = fields.Char(
        string="Orígenes de cambio que no facturan",
        config_parameter="hubspot_invoice_bridge.ignored_change_sources",
        default="IMPORT",
        help="HubSpot dispara el mismo evento cuando alguien mueve un negocio "
        "a mano y cuando una importación reescribe mil de golpe. Lo segundo no "
        "debería generar mil facturas.",
    )

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

    hubspot_sweep_enabled = fields.Boolean(
        string="Buscar los negocios ganados periódicamente",
        config_parameter="hubspot_invoice_bridge.sweep_enabled",
        help="Además de escuchar los webhooks, pregunta cada cuarto de hora "
        "por los negocios que ya están en una etapa que factura. Es el "
        "respaldo de la suscripción de HubSpot, que desde Odoo no se "
        "puede comprobar: si se pausa o se borra, sin esto el silencio "
        "es idéntico al de un día sin ventas.",
    )
    hubspot_sweep_since = fields.Datetime(
        string="Buscar a partir de",
        config_parameter="hubspot_invoice_bridge.sweep_since",
        help="Suelo del barrido: no se mira nada modificado antes de esta "
        "fecha. Se fija solo la primera vez, con la fecha de entonces, "
        "para no arrastrar de golpe el histórico entero del portal.",
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
