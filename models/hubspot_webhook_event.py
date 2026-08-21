import json
import logging
from datetime import datetime, timezone

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Propiedades que se piden a HubSpot para cada tipo de objeto.
# Los nombres internos provienen del inventario de propiedades del portal
# (resources/Propiedades de HubSpot .xlsx). Las que no son estándar de HubSpot
# van marcadas como personalizadas.
DEAL_PROPERTIES = [
    "dealname",
    "amount",
    "dealstage",
    "pipeline",
    "closedate",
    "hubspot_owner_id",
    "deal_currency_code",
    "description",
    "categoria_de_negocio",  # personalizada: Software / Hardware / ...
    "cotizaciones_aprobadas",  # personalizada: números de cotización
    "licencia_elastica",  # personalizada: booleana
    "modalidad_licencia_anual",  # personalizada: booleana
]
COMPANY_PROPERTIES = [
    "name",
    "domain",
    "address",
    "city",
    "state",
    "zip",
    "country",
    "phone",
    "industry",
    "numberofemployees",
    "nit",  # personalizada: NIT → res.partner.vat
    "correo_de_facturacion",  # personalizada: correo de facturación
    "correo_del_representante_legal",  # personalizada
    "tipo_de_empresa",  # personalizada
]
CONTACT_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "phone",
    "mobilephone",
    "jobtitle",
]
LINE_ITEM_PROPERTIES = [
    "name",
    "price",
    "quantity",
    "amount",
    "hs_product_id",
    "hs_sku",
    "description",
    "discount",
    "hs_discount_percentage",
    "hs_line_item_currency_code",
]

# Número de intentos antes de dejar de reprocesar un evento en error.
MAX_ATTEMPTS = 5


class HubspotWebhookEvent(models.Model):
    """Cola de eventos recibidos de HubSpot.

    El controller sólo persiste aquí y responde 200 de inmediato: HubSpot
    exige respuesta en unos 5 segundos y reintenta durante 24 h si no la
    recibe. Todo el trabajo real (llamadas a la API, creación de la factura)
    ocurre después, en el cron.
    """

    _name = "hubspot.webhook.event"
    _description = "Evento de webhook de HubSpot"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "occurred_at desc, id desc"

    hubspot_event_id = fields.Char(
        string="ID de evento", required=True, index=True, readonly=True
    )
    subscription_type = fields.Char(string="Tipo de suscripción", readonly=True)
    object_id = fields.Char(string="ID de negocio", index=True, readonly=True)
    property_name = fields.Char(string="Propiedad", readonly=True)
    property_value = fields.Char(string="Valor nuevo", readonly=True)
    occurred_at = fields.Datetime(string="Ocurrido el", readonly=True)
    portal_id = fields.Char(string="Portal", readonly=True)
    payload = fields.Text(string="Payload recibido", readonly=True)

    state = fields.Selection(
        [
            ("pending", "Pendiente"),
            ("skipped", "Descartado"),
            ("done", "Procesado"),
            ("error", "Error"),
        ],
        string="Estado",
        default="pending",
        required=True,
        index=True,
        readonly=True,
        tracking=True,
    )
    error_message = fields.Text(string="Detalle del error", readonly=True)
    attempts = fields.Integer(string="Intentos", default=0, readonly=True)
    processed_at = fields.Datetime(string="Procesado el", readonly=True)

    move_id = fields.Many2one(
        "account.move", string="Factura generada", readonly=True, ondelete="set null"
    )
    partner_id = fields.Many2one(
        "res.partner", string="Cliente", readonly=True, ondelete="set null"
    )

    _sql_constraints = [
        (
            "event_uniq",
            "unique(hubspot_event_id)",
            "Este evento de HubSpot ya fue registrado.",
        ),
    ]

    def _compute_display_name(self):
        for event in self:
            event.display_name = _(
                "Negocio %(deal)s → %(stage)s",
                deal=event.object_id or "?",
                stage=event.property_value or "?",
            )

    # ------------------------------------------------------------------
    # Ingesta
    # ------------------------------------------------------------------

    @api.model
    def ingest_batch(self, events):
        """Persiste el lote recibido descartando los duplicados.

        Se ejecuta dentro de la petición del webhook, así que debe ser barato:
        nada de llamadas a la API de HubSpot aquí.
        """
        created = self.browse()
        for raw in events:
            event_id = str(raw.get("eventId") or "")
            if not event_id:
                _logger.warning("Evento de HubSpot sin eventId, descartado: %s", raw)
                continue

            occurred_at = False
            if raw.get("occurredAt"):
                try:
                    # 'occurredAt' llega en milisegundos UTC. Odoo almacena los
                    # datetime en UTC y sin zona horaria, con precisión de
                    # segundo.
                    occurred_at = datetime.fromtimestamp(
                        int(raw["occurredAt"]) / 1000.0, tz=timezone.utc
                    ).replace(tzinfo=None, microsecond=0)
                except (TypeError, ValueError, OSError, OverflowError):
                    occurred_at = False

            vals = {
                "hubspot_event_id": event_id,
                "subscription_type": raw.get("subscriptionType"),
                "object_id": str(raw.get("objectId") or ""),
                "property_name": raw.get("propertyName"),
                "property_value": raw.get("propertyValue"),
                "occurred_at": occurred_at,
                "portal_id": str(raw.get("portalId") or ""),
                "payload": json.dumps(raw, indent=2, ensure_ascii=False),
            }

            # Un savepoint por evento: si HubSpot reenvía uno ya registrado,
            # la restricción única lo rechaza sin abortar el resto del lote.
            try:
                with self.env.cr.savepoint():
                    created |= self.create(vals)
            except Exception:  # noqa: BLE001 - incluye IntegrityError del unique
                _logger.info(
                    "Evento de HubSpot %s ya registrado, se ignora el duplicado.",
                    event_id,
                )
        return created

    # ------------------------------------------------------------------
    # Procesamiento diferido
    # ------------------------------------------------------------------

    @api.model
    def _cron_process_events(self, limit=50):
        events = self.search(
            [
                ("state", "in", ("pending", "error")),
                ("attempts", "<", MAX_ATTEMPTS),
            ],
            order="occurred_at asc, id asc",
            limit=limit,
        )
        for event in events:
            event._process_one()
            # Un commit por evento: un fallo posterior no revierte las
            # facturas ya creadas correctamente.
            self.env.cr.commit()
        return True

    def action_process(self):
        """Reprocesa manualmente desde la vista."""
        for event in self:
            event._process_one()
        return True

    def action_reset_to_pending(self):
        self.write({"state": "pending", "attempts": 0, "error_message": False})
        return True

    def _process_one(self):
        self.ensure_one()
        self.attempts += 1
        try:
            with self.env.cr.savepoint():
                result = self._do_process()
        except Exception as err:  # noqa: BLE001 - se registra y se reintenta
            _logger.exception(
                "Error procesando el evento de HubSpot %s", self.hubspot_event_id
            )
            message = str(err) or err.__class__.__name__
            self.write(
                {
                    "state": "error",
                    "error_message": message,
                    "processed_at": fields.Datetime.now(),
                }
            )
            self._notify_failure(message)
            return False

        self.write(dict(result, processed_at=fields.Datetime.now()))
        return True

    def _do_process(self):
        """Lógica de negocio. Devuelve los valores de cierre del evento."""
        self.ensure_one()

        if self.property_name != "dealstage":
            return {
                "state": "skipped",
                "error_message": _(
                    "La propiedad '%s' no es 'dealstage'.", self.property_name or ""
                ),
            }

        if not self.env["hubspot.deal.stage"].stage_triggers_invoice(
            self.property_value
        ):
            return {
                "state": "skipped",
                "error_message": _(
                    "La etapa '%s' no genera factura.", self.property_value or ""
                ),
            }

        # Idempotencia de negocio: aunque lleguen dos eventos distintos para el
        # mismo negocio (reintentos, idas y venidas entre etapas), sólo se
        # factura una vez.
        existing = (
            self.env["account.move"]
            .sudo()
            .search([("hubspot_deal_id", "=", self.object_id)], limit=1)
        )
        if existing:
            return {
                "state": "done",
                "move_id": existing.id,
                "partner_id": existing.partner_id.id,
                "error_message": _(
                    "El negocio ya tenía la factura %s; no se duplica.",
                    existing.name or existing.display_name,
                ),
            }

        move = self._create_invoice()
        return {
            "state": "done",
            "move_id": move.id,
            "partner_id": move.partner_id.id,
            "error_message": False,
        }

    # ------------------------------------------------------------------
    # Construcción de la factura
    # ------------------------------------------------------------------

    def _create_invoice(self):
        self.ensure_one()
        client = self.env["hubspot.client"]
        deal_id = self.object_id

        deal = client.get_object("deals", deal_id, properties=DEAL_PROPERTIES)
        if not deal:
            raise UserError(
                _(
                    "El negocio %s no existe en HubSpot o el token no tiene "
                    "acceso a él.",
                    deal_id,
                )
            )
        deal_props = deal.get("properties") or {}

        mapping = self._resolve_location(deal_props)
        partner = self._resolve_partner(deal_id)
        lines = self._build_invoice_lines(deal_id, mapping.company_id)

        currency = self._resolve_currency(deal_props, mapping)

        move_vals = {
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "company_id": mapping.company_id.id,
            "journal_id": mapping.journal_id.id,
            "currency_id": currency.id,
            "invoice_origin": _("HubSpot %s", deal_props.get("dealname") or deal_id),
            "ref": deal_props.get("dealname") or False,
            "hubspot_deal_id": deal_id,
            "hubspot_portal_id": self.portal_id or False,
            "invoice_line_ids": [Command.create(line) for line in lines],
        }
        if mapping.fiscal_position_id:
            move_vals["fiscal_position_id"] = mapping.fiscal_position_id.id

        narration = self._prepare_narration(deal_props)
        if narration:
            move_vals["narration"] = narration

        move = (
            self.env["account.move"].with_company(mapping.company_id).create(move_vals)
        )

        # La factura se deja SIEMPRE en borrador: falta completar datos que se
        # editan a mano (fecha, referencias fiscales, revisión del cliente).
        move.message_post(
            body=_(
                "Factura generada automáticamente desde el negocio de HubSpot "
                "<b>%(name)s</b> (id %(id)s).",
                name=deal_props.get("dealname") or "—",
                id=deal_id,
            )
        )

        self._writeback_to_hubspot(deal_id, move)
        return move

    def _prepare_narration(self, deal_props):
        """Nota interna con el contexto comercial del negocio.

        No se vuelca en campos contables porque son datos de CRM, pero le
        ahorran a quien revisa la factura tener que abrir HubSpot.
        """
        parts = []
        if deal_props.get("cotizaciones_aprobadas"):
            parts.append(
                _("Cotizaciones aprobadas: %s", deal_props["cotizaciones_aprobadas"])
            )
        if deal_props.get("categoria_de_negocio"):
            parts.append(
                _("Categoría de negocio: %s", deal_props["categoria_de_negocio"])
            )
        if str(deal_props.get("licencia_elastica", "")).lower() == "true":
            parts.append(_("Incluye licencia elástica."))
        if str(deal_props.get("modalidad_licencia_anual", "")).lower() == "true":
            parts.append(_("Modalidad de licencia anual."))
        if deal_props.get("closedate"):
            parts.append(_("Fecha de cierre en HubSpot: %s", deal_props["closedate"]))
        return "<br/>".join(parts) if parts else False

    def _resolve_location(self, deal_props):
        """Compañía / diario / posición fiscal a partir de la propiedad de
        localización del negocio."""
        param = self.env["ir.config_parameter"].sudo()
        # Por defecto se enruta por el pipeline del negocio, que ya distingue
        # las líneas de negocio existentes (ventas, JoobPay). Cuando exista la
        # propiedad de localización dedicada en HubSpot, basta con poner aquí su
        # nombre interno; no hay que tocar código.
        property_name = param.get_param(
            "hubspot_invoice_bridge.location_property", "pipeline"
        )
        value = deal_props.get(property_name) if property_name else None

        mapping = self.env["hubspot.location.mapping"].resolve(value)
        if not mapping:
            raise UserError(
                _(
                    "No hay ningún mapeo de localización aplicable "
                    "(valor recibido: '%(value)s') ni una línea predeterminada "
                    "configurada. Revise Contabilidad → HubSpot → Localizaciones.",
                    value=value or "",
                )
            )
        return mapping

    def _resolve_currency(self, deal_props, mapping):
        code = (deal_props.get("deal_currency_code") or "").strip().upper()
        if code:
            currency = (
                self.env["res.currency"]
                .with_context(active_test=False)
                .search([("name", "=", code)], limit=1)
            )
            if currency and currency.active:
                return currency
            if currency and not currency.active:
                _logger.warning(
                    "La moneda %s del negocio %s existe en Odoo pero está "
                    "archivada; se usa la de respaldo.",
                    code,
                    self.object_id,
                )
            else:
                _logger.warning(
                    "La moneda %s del negocio %s no existe en Odoo; se usa la "
                    "de respaldo.",
                    code,
                    self.object_id,
                )
        return mapping.currency_id or mapping.company_id.currency_id

    # ------------------------------------------------------------------
    # Cliente
    # ------------------------------------------------------------------

    def _resolve_partner(self, deal_id):
        """El cliente de la factura es la EMPRESA asociada al negocio."""
        client = self.env["hubspot.client"]
        associations = client.get_associated_ids("deals", deal_id, "companies")
        if not associations:
            raise UserError(
                _(
                    "El negocio %s no tiene ninguna empresa asociada en HubSpot, "
                    "así que no se puede determinar el cliente a facturar. "
                    "Asocie la empresa en HubSpot y reprocese este evento.",
                    deal_id,
                )
            )

        # Si hay varias, se prefiere la marcada como primaria.
        primary = [cid for cid, is_primary in associations if is_primary]
        company_id = primary[0] if primary else associations[0][0]
        if len(associations) > 1:
            _logger.warning(
                "El negocio %s tiene %s empresas asociadas; se factura a %s.",
                deal_id,
                len(associations),
                company_id,
            )

        partner = self.env["res.partner"].search(
            [("hubspot_company_id", "=", company_id)], limit=1
        )
        if partner:
            return partner

        company = client.get_object(
            "companies", company_id, properties=COMPANY_PROPERTIES
        )
        if not company:
            raise UserError(
                _(
                    "La empresa %s asociada al negocio no se pudo leer de HubSpot.",
                    company_id,
                )
            )

        vals = self._prepare_partner_vals(company_id, company.get("properties") or {})
        try:
            with self.env.cr.savepoint():
                partner = self.env["res.partner"].create(vals)
        except ValidationError as err:
            # Con base_vat instalado, un NIT con formato inesperado invalida la
            # creación entera. Se crea el cliente sin identificación fiscal y se
            # deja constancia, en vez de bloquear la factura.
            if not vals.get("vat"):
                raise
            rejected_vat = vals.pop("vat")
            _logger.warning(
                "El NIT '%s' de la empresa %s fue rechazado por Odoo (%s); "
                "se crea el cliente sin identificación fiscal.",
                rejected_vat,
                company_id,
                err,
            )
            partner = self.env["res.partner"].create(vals)
            partner.message_post(
                body=_(
                    "El NIT recibido de HubSpot (<b>%(vat)s</b>) no pasó la "
                    "validación de Odoo y no se asignó. Complételo a mano antes "
                    "de validar la factura.",
                    vat=rejected_vat,
                )
            )

        _logger.info(
            "Creado el cliente %s (id %s) desde la empresa %s de HubSpot.",
            partner.display_name,
            partner.id,
            company_id,
        )

        self._sync_primary_contact(deal_id, partner)
        return partner

    def _prepare_partner_vals(self, company_id, props):
        param = self.env["ir.config_parameter"].sudo()
        vat_property = param.get_param("hubspot_invoice_bridge.vat_property", "nit")
        email_property = param.get_param(
            "hubspot_invoice_bridge.billing_email_property", "correo_de_facturacion"
        )

        vals = {
            "name": props.get("name") or _("Empresa HubSpot %s", company_id),
            "is_company": True,
            "company_type": "company",
            "hubspot_company_id": company_id,
            "street": props.get("address") or False,
            "city": props.get("city") or False,
            "zip": props.get("zip") or False,
            "phone": props.get("phone") or False,
            "website": props.get("domain") or False,
        }
        # NIT / identificación tributaria. Odoo valida el formato según el país,
        # así que un valor mal formado haría fallar la creación del cliente; se
        # asigna aparte para poder degradar con aviso en vez de romper.
        if vat_property and props.get(vat_property):
            vals["vat"] = str(props[vat_property]).strip()
        if email_property and props.get(email_property):
            vals["email"] = str(props[email_property]).strip()

        country = self._find_country(props.get("country"))
        if country:
            vals["country_id"] = country.id
            state = self._find_state(props.get("state"), country)
            if state:
                vals["state_id"] = state.id
        return vals

    def _find_country(self, name):
        if not name:
            return self.env["res.country"].browse()
        name = name.strip()
        Country = self.env["res.country"]
        # HubSpot entrega el país como texto libre; se prueba por código y por
        # nombre antes de rendirse.
        if len(name) == 2:
            country = Country.search([("code", "=ilike", name)], limit=1)
            if country:
                return country
        return Country.search([("name", "=ilike", name)], limit=1)

    def _find_state(self, name, country):
        if not name or not country:
            return self.env["res.country.state"].browse()
        State = self.env["res.country.state"]
        domain = [("country_id", "=", country.id)]
        return State.search(
            domain + [("name", "=ilike", name.strip())], limit=1
        ) or State.search(domain + [("code", "=ilike", name.strip())], limit=1)

    def _sync_primary_contact(self, deal_id, partner):
        """Crea el contacto principal del negocio como hijo del cliente.

        Es opcional: sirve para tener a quién enviar la factura. Un fallo aquí
        no debe impedir la facturación.
        """
        param = self.env["ir.config_parameter"].sudo()
        if param.get_param("hubspot_invoice_bridge.sync_contacts", "True") == "False":
            return

        try:
            client = self.env["hubspot.client"]
            associations = client.get_associated_ids("deals", deal_id, "contacts")
            if not associations:
                return
            primary = [cid for cid, is_primary in associations if is_primary]
            contact_id = primary[0] if primary else associations[0][0]

            if self.env["res.partner"].search_count(
                [("hubspot_contact_id", "=", contact_id)]
            ):
                return

            contact = client.get_object(
                "contacts", contact_id, properties=CONTACT_PROPERTIES
            )
            if not contact:
                return
            props = contact.get("properties") or {}
            name = " ".join(
                filter(None, [props.get("firstname"), props.get("lastname")])
            ).strip()
            if not name:
                name = props.get("email") or _("Contacto HubSpot %s", contact_id)

            self.env["res.partner"].create(
                {
                    "name": name,
                    "parent_id": partner.id,
                    "type": "contact",
                    "is_company": False,
                    "email": props.get("email") or False,
                    "phone": props.get("phone") or False,
                    "function": props.get("jobtitle") or False,
                    "hubspot_contact_id": contact_id,
                }
            )
        except Exception:  # noqa: BLE001 - accesorio, no debe romper la factura
            _logger.exception(
                "No se pudo sincronizar el contacto principal del negocio %s", deal_id
            )

    # ------------------------------------------------------------------
    # Líneas
    # ------------------------------------------------------------------

    def _build_invoice_lines(self, deal_id, company):
        client = self.env["hubspot.client"]
        associations = client.get_associated_ids("deals", deal_id, "line_items")
        if not associations:
            raise UserError(
                _(
                    "El negocio %s no tiene productos (line items) asociados en "
                    "HubSpot, así que no hay nada que facturar.",
                    deal_id,
                )
            )

        line_item_ids = [lid for lid, _primary in associations]
        line_items = client.batch_read(
            "line_items", line_item_ids, LINE_ITEM_PROPERTIES
        )

        lines = []
        unmatched = []
        for item in line_items:
            props = item.get("properties") or {}
            product = self._resolve_product(props, company)
            if not product:
                unmatched.append(
                    "%s (SKU: %s, hs_product_id: %s)"
                    % (
                        props.get("name") or "?",
                        props.get("hs_sku") or "—",
                        props.get("hs_product_id") or "—",
                    )
                )
                continue

            quantity = self._to_float(props.get("quantity"), default=1.0)
            price_unit = self._to_float(props.get("price"))
            lines.append(
                {
                    "product_id": product.id,
                    "name": (props.get("name") or product.display_name),
                    "quantity": quantity,
                    "price_unit": price_unit,
                    "discount": self._resolve_discount(props, quantity, price_unit),
                }
            )

        if unmatched:
            # Se aborta entera: una factura a medias es peor que ninguna.
            raise UserError(
                _(
                    "No se pudieron homologar estos productos de HubSpot con "
                    "productos de Odoo:\n\n%(items)s\n\n"
                    "Homologue los productos y reprocese el evento.",
                    items="\n".join("• %s" % u for u in unmatched),
                )
            )

        return lines

    def _resolve_product(self, props, company):
        """Homologación HubSpot → Odoo.

        Orden de búsqueda: id de producto de HubSpot ya vinculado → SKU →
        referencia interna → nombre exacto. Cuando se encuentra por SKU o
        nombre, se guarda el ``hs_product_id`` para que la siguiente vez sea
        una búsqueda directa.
        """
        Product = self.env["product.product"].with_company(company)
        domain_company = [
            "|",
            ("company_id", "=", False),
            ("company_id", "=", company.id),
        ]

        hs_product_id = props.get("hs_product_id")
        if hs_product_id:
            product = Product.search(
                [("hubspot_product_id", "=", str(hs_product_id))] + domain_company,
                limit=1,
            )
            if product:
                return product

        sku = (props.get("hs_sku") or "").strip()
        if sku:
            product = Product.search(
                [("default_code", "=", sku)] + domain_company, limit=1
            )
            if not product:
                product = Product.search(
                    [("barcode", "=", sku)] + domain_company, limit=1
                )
            if product:
                self._remember_product_link(product, hs_product_id)
                return product

        name = (props.get("name") or "").strip()
        if name:
            product = Product.search([("name", "=", name)] + domain_company, limit=1)
            if product:
                self._remember_product_link(product, hs_product_id)
                return product

        return Product.browse()

    def _resolve_discount(self, props, quantity, price_unit):
        """Descuento de la línea, en porcentaje (que es lo que espera Odoo).

        HubSpot expone dos propiedades y no siempre las dos están pobladas:

        * ``hs_discount_percentage`` — porcentaje. Se usa tal cual.
        * ``discount`` — en la semántica estándar de HubSpot es un **importe**,
          pero el inventario de propiedades del portal lo documenta como
          porcentaje y marca la duda expresamente. Por eso la interpretación se
          controla con el parámetro ``hubspot_invoice_bridge.discount_is_percentage``
          (por defecto ``False``, es decir, importe) en lugar de asumirla.
        """
        percentage = props.get("hs_discount_percentage")
        if percentage not in (None, "", False):
            return self._clamp_discount(self._to_float(percentage))

        raw = props.get("discount")
        if raw in (None, "", False):
            return 0.0

        value = self._to_float(raw)
        if not value:
            return 0.0

        is_percentage = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.discount_is_percentage", "False")
            == "True"
        )
        if is_percentage:
            return self._clamp_discount(value)

        gross = quantity * price_unit
        if not gross:
            return 0.0
        return self._clamp_discount(value / gross * 100.0)

    @staticmethod
    def _clamp_discount(value):
        return min(max(value, 0.0), 100.0)

    def _remember_product_link(self, product, hs_product_id):
        if hs_product_id and not product.hubspot_product_id:
            product.sudo().hubspot_product_id = str(hs_product_id)

    @staticmethod
    def _to_float(value, default=0.0):
        if value in (None, "", False):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Escritura de vuelta y avisos
    # ------------------------------------------------------------------

    def _writeback_to_hubspot(self, deal_id, move):
        """Opcional: deja constancia en el negocio de la factura creada."""
        param = self.env["ir.config_parameter"].sudo()
        property_name = param.get_param("hubspot_invoice_bridge.writeback_property", "")
        if not property_name:
            return
        try:
            base_url = param.get_param("web.base.url", "")
            value = (
                "%s/odoo/action-account.action_move_out_invoice_type/%s"
                % (base_url.rstrip("/"), move.id)
                if base_url
                else str(move.id)
            )
            self.env["hubspot.client"].update_deal_properties(
                deal_id, {property_name: value}
            )
        except Exception:  # noqa: BLE001 - accesorio
            _logger.exception(
                "No se pudo escribir la referencia de la factura en el negocio %s",
                deal_id,
            )

    def _notify_failure(self, message):
        """Avisa al responsable configurado cuando un evento queda en error."""
        self.ensure_one()
        param = self.env["ir.config_parameter"].sudo()
        user_id = param.get_param("hubspot_invoice_bridge.responsible_user_id", "")
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return
        user = self.env["res.users"].browse(user_id).exists()
        if not user:
            return

        # Una sola actividad por evento, se actualiza en cada reintento.
        activity = self.activity_ids.filtered(lambda a: a.user_id == user)[:1]
        summary = _("Negocio %s de HubSpot sin facturar", self.object_id or "?")
        if activity:
            activity.note = message
        else:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=user.id,
                summary=summary,
                note=message,
            )
