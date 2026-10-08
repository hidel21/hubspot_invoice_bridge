import json
import logging
import re
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
# Nombres tomados del inventario de propiedades de cotización de HubSpot, no
# supuestos: la firma no vive en una sola propiedad. Una cotización puede
# quedar firmada a mano (`hs_manually_signed`) o electrónicamente, y en ese
# caso lo que hay que mirar es cuántos firmantes han completado frente a
# cuántos se exigían.
QUOTE_PROPERTIES = [
    "hs_title",
    "hs_status",
    "hs_quote_number",
    "hs_quote_amount",
    "hs_expiration_date",
    "hs_payment_status",
    "hs_quote_esign_status",
    "hs_esign_enabled",
    "hs_esign_num_signers_required",
    "hs_esign_num_signers_completed",
    "hs_manually_signed",
    "hubspot_owner_id",
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
    # HubSpot dispara el evento también al CREAR un negocio que nace con una
    # etapa asignada. changeSource es lo que distingue esa alta de una
    # transición real, y por eso se guarda aunque hoy solo se consulte para
    # descartar orígenes concretos.
    change_source = fields.Char(string="Origen del cambio", readonly=True)
    # Las suscripciones genéricas —las que permiten escuchar cotizaciones—
    # llegan como «object.propertyChange» y dicen de qué objeto hablan aquí.
    object_type_id = fields.Char(string="Tipo de objeto", readonly=True)
    deal_id = fields.Char(
        string="Negocio resuelto",
        readonly=True,
        help="Cuando el evento es de una cotización, el negocio al que "
        "pertenece. Para los eventos de negocio coincide con el objeto.",
    )
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
    sale_order_id = fields.Many2one(
        "sale.order",
        string="Orden de venta generada",
        readonly=True,
        ondelete="set null",
        help="Se genera en lugar de la factura cuando el negocio lleva "
        "producto almacenable: es la orden la que mueve el inventario.",
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
                "change_source": raw.get("changeSource"),
                "object_type_id": str(raw.get("objectTypeId") or ""),
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

    @api.model
    def _cron_sweep_deals(self, limit=25):
        """Va a buscar los negocios ganados en vez de esperar a que avisen.

        El webhook es la vía rápida, pero depende de una suscripción que vive
        dentro de la app privada de HubSpot y que desde Odoo no se puede leer
        ni comprobar: el token da 403 en la API de webhooks. Si alguien la
        pausa, la cambia o nunca llegó a crearse, el silencio es exactamente
        igual al de un día sin ventas, y así se pueden perder semanas sin que
        nadie lo note.

        Este barrido cierra ese agujero preguntando por los negocios que ya
        están en una etapa que factura. No sustituye al webhook: lo respalda.

        No duplica nada. El identificador del evento lleva la fecha de
        modificación del negocio, así que repetir el mismo negocio sin
        cambios choca contra la restricción única, y el procesado ya se niega
        a crear un segundo documento para un negocio que ya tiene uno.
        """
        param = self.env["ir.config_parameter"].sudo()
        if param.get_param("hubspot_invoice_bridge.sweep_enabled") != "True":
            return 0

        etapas = self.env["hubspot.deal.stage"].search(
            [("trigger_invoice", "=", True)]
        )
        if not etapas:
            _logger.info(
                "Barrido de HubSpot: ninguna etapa marcada para facturar, "
                "no hay nada que buscar."
            )
            return 0

        # El suelo se fija la primera vez y no se mueve: el barrido sirve para
        # lo que venga a partir de ahora, no para arrastrar el histórico
        # entero del portal de golpe.
        suelo = param.get_param("hubspot_invoice_bridge.sweep_since")
        if not suelo:
            suelo = fields.Datetime.to_string(fields.Datetime.now())
            param.set_param("hubspot_invoice_bridge.sweep_since", suelo)
        desde = param.get_param("hubspot_invoice_bridge.sweep_cursor") or suelo

        marca = int(
            fields.Datetime.from_string(desde)
            .replace(tzinfo=timezone.utc)
            .timestamp()
            * 1000
        )

        respuesta = self.env["hubspot.client"]._request(
            "POST",
            "/crm/v3/objects/deals/search",
            payload={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "dealstage",
                                "operator": "IN",
                                "values": etapas.mapped("stage_id"),
                            },
                            {
                                "propertyName": "hs_lastmodifieddate",
                                "operator": "GTE",
                                "value": str(marca),
                            },
                        ]
                    }
                ],
                "properties": ["dealstage", "hs_lastmodifieddate"],
                "sorts": [
                    {
                        "propertyName": "hs_lastmodifieddate",
                        "direction": "ASCENDING",
                    }
                ],
                "limit": limit,
            },
        )
        negocios = (respuesta or {}).get("results") or []
        if not negocios:
            return 0

        eventos = []
        ultima = None
        for negocio in negocios:
            props = negocio.get("properties") or {}
            modificado = self._hubspot_datetime(props.get("hs_lastmodifieddate"))
            if modificado:
                ultima = modificado
            eventos.append(
                {
                    "eventId": "barrido-%s-%s"
                    % (negocio.get("id"), props.get("hs_lastmodifieddate") or ""),
                    "subscriptionType": "deal.propertyChange",
                    "objectId": negocio.get("id"),
                    "propertyName": "dealstage",
                    "propertyValue": props.get("dealstage"),
                    "changeSource": "BARRIDO",
                    "occurredAt": int(
                        modificado.replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                    if modificado
                    else None,
                }
            )

        creados = self.ingest_batch(eventos)

        # El cursor avanza hasta la última fecha vista, no un segundo más: el
        # filtro vuelve a incluirla en la pasada siguiente y el duplicado se
        # descarta solo. Prefiero repetir uno a saltarme otro.
        if ultima:
            param.set_param(
                "hubspot_invoice_bridge.sweep_cursor",
                fields.Datetime.to_string(ultima),
            )

        _logger.info(
            "Barrido de HubSpot: %d negocio(s) revisados desde %s, %d evento(s) "
            "nuevos en cola.",
            len(negocios),
            desde,
            len(creados),
        )
        return len(creados)

    @staticmethod
    def _hubspot_datetime(valor):
        """'2026-10-06T18:05:31.123Z' → datetime en UTC y sin zona."""
        if not valor:
            return None
        try:
            return (
                datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .replace(tzinfo=None, microsecond=0)
            )
        except (TypeError, ValueError):
            return None

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
        """Lógica de negocio. Devuelve los valores de cierre del evento.

        El evento solo sirve para despertar: la decisión se toma leyendo el
        **estado actual** del negocio en HubSpot, no lo que traía el webhook.
        Es lo que hace que el orden de llegada deje de importar. Si facturar
        exige etapa ganada *y* cotización firmada, da igual cuál de las dos
        ocurra primero: cuando llegue la segunda, la consulta verá las dos.
        """
        self.ensure_one()

        origen_ignorado = self._change_source_ignored()
        if origen_ignorado:
            return {
                "state": "skipped",
                "error_message": _(
                    "Origen del cambio '%s': no se factura.", origen_ignorado
                ),
            }

        deal_id = self._resolve_deal_id()
        if not deal_id:
            return {
                "state": "skipped",
                "error_message": _(
                    "El evento no corresponde a un negocio ni a una cotización "
                    "asociada a uno (tipo '%s', objeto '%s').",
                    self.subscription_type or "?",
                    self.object_type_id or "?",
                ),
            }
        if deal_id != self.deal_id:
            self.deal_id = deal_id

        # Idempotencia de negocio: aunque lleguen dos eventos distintos para el
        # mismo negocio (reintentos, idas y venidas entre etapas, un cambio en
        # su cotización), sólo se factura una vez.
        # Se miran los dos documentos posibles: un negocio puede haber
        # generado una orden de venta en lugar de una factura.
        existing = (
            self.env["account.move"]
            .sudo()
            .search([("hubspot_deal_id", "=", deal_id)], limit=1)
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
        existing_so = (
            self.env["sale.order"]
            .sudo()
            .search([("hubspot_deal_id", "=", deal_id)], limit=1)
        )
        if existing_so:
            return {
                "state": "done",
                "sale_order_id": existing_so.id,
                "partner_id": existing_so.partner_id.id,
                "error_message": _(
                    "El negocio ya tenía la orden de venta %s; no se duplica.",
                    existing_so.name or existing_so.display_name,
                ),
            }

        client = self.env["hubspot.client"]
        deal = client.get_object("deals", deal_id, properties=DEAL_PROPERTIES)
        if not deal:
            return {
                "state": "error",
                "error_message": _(
                    "El negocio %s no existe en HubSpot o el token no tiene "
                    "acceso a él.",
                    deal_id,
                ),
            }
        deal_props = deal.get("properties") or {}

        motivo, quote_id = self._invoice_reason(deal_id, deal_props)
        if not motivo:
            return {
                "state": "skipped",
                "error_message": _(
                    "El negocio %s todavía no cumple la condición para " "facturar.",
                    deal_id,
                ),
            }

        documento = self._create_document(deal_id, deal_props, quote_id=quote_id)
        documento.message_post(body=_("Generado desde HubSpot: %s", motivo))
        resultado = {
            "state": "done",
            "partner_id": documento.partner_id.id,
            "error_message": False,
        }
        if documento._name == "sale.order":
            resultado["sale_order_id"] = documento.id
        else:
            resultado["move_id"] = documento.id
        return resultado

    # ------------------------------------------------------------------
    # Qué evento nos despierta y sobre qué negocio
    # ------------------------------------------------------------------

    def _change_source_ignored(self):
        """Orígenes de cambio que no deben facturar.

        HubSpot dispara el mismo evento cuando alguien mueve un negocio a mano
        y cuando una importación masiva reescribe mil de golpe. Lo segundo no
        debería generar mil facturas.
        """
        crudos = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.ignored_change_sources", "IMPORT")
        )
        ignorados = {x.strip().upper() for x in crudos.split(",") if x.strip()}
        actual = (self.change_source or "").upper()
        return actual if actual and actual in ignorados else False

    def _resolve_deal_id(self):
        """El negocio del que habla este evento.

        Puede venir por tres caminos: la suscripción clásica ``deal.*``, la
        genérica ``object.*`` con su ``objectTypeId``, o un evento de
        cotización —del que hay que subir al negocio asociado, porque es ahí
        donde vive la factura—.
        """
        tipo = (self.subscription_type or "").lower()
        deals_type = self._object_type_id("deals", "0-3")
        quotes_type = self._object_type_id("quotes", "0-14")

        es_negocio = tipo.startswith("deal.") or self.object_type_id == deals_type
        es_cotizacion = tipo.startswith("quote.") or self.object_type_id == quotes_type

        if es_negocio:
            return self.object_id
        if es_cotizacion:
            asociados = self.env["hubspot.client"].get_associated_ids(
                "quotes", self.object_id, "deals"
            )
            if not asociados:
                return False
            # La primaria si la hay; si no, la primera que devuelva HubSpot.
            primaria = next((i for i, es_primaria in asociados if es_primaria), None)
            return primaria or asociados[0][0]
        # Sin tipo reconocible se asume negocio, que es lo que llegaba antes
        # de existir las suscripciones genéricas.
        return self.object_id if not self.object_type_id else False

    def _object_type_id(self, nombre, por_defecto):
        """Identificador de tipo de objeto, configurable.

        HubSpot los documenta en una tabla aparte y los ha ido ampliando; no
        se escriben en el código para que un cambio suyo no obligue a tocarlo.
        """
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.object_type_%s" % nombre, por_defecto)
        )

    # ------------------------------------------------------------------
    # La condición para facturar
    # ------------------------------------------------------------------

    def _invoice_reason(self, deal_id, deal_props):
        """¿Toca facturar este negocio? Devuelve ``(motivo, quote_id)``.

        El motivo se escribe después en el historial de la factura: dentro de
        seis meses, saber por qué se emitió vale más que el hecho de que se
        emitiera.
        """
        modo = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.trigger_mode", "stage")
        )

        etapa_ok = self._stage_is_won(deal_props)
        marca_ok = self._deal_flag_active(deal_props)
        quote_id = False
        if modo in ("quote", "stage_and_quote"):
            quote_id = self._signed_quote(deal_id)

        if modo == "stage":
            return (
                _("negocio en etapa ganada (%s)", deal_props.get("dealstage") or "?")
                if etapa_ok
                else False
            ), quote_id
        if modo == "flag":
            return (
                _("cotización aprobada en el negocio") if marca_ok else False
            ), quote_id
        if modo == "quote":
            return (
                _("cotización %s firmada", quote_id) if quote_id else False
            ), quote_id
        if modo == "stage_and_quote":
            if etapa_ok and quote_id:
                return _("etapa ganada y cotización %s firmada", quote_id), quote_id
            return False, quote_id
        if modo == "stage_or_flag":
            if etapa_ok:
                return _("negocio en etapa ganada"), quote_id
            if marca_ok:
                return _("cotización aprobada en el negocio"), quote_id
            return False, quote_id

        _logger.warning(
            "hubspot: modo de disparo '%s' desconocido; no se factura.", modo
        )
        return False, quote_id

    def _stage_is_won(self, deal_props):
        return self.env["hubspot.deal.stage"].stage_triggers_invoice(
            deal_props.get("dealstage")
        )

    def _deal_flag_active(self, deal_props):
        """¿El negocio declara alguna cotización aprobada?

        `cotizaciones_aprobadas` **no es un sí o un no**: lleva escritas cuáles
        son las cotizaciones aprobadas, una o varias separadas por coma. Que
        tenga algo escrito es la aprobación.

        Se siguen admitiendo los valores booleanos por si algún portal usa la
        propiedad de la otra forma.
        """
        propiedad = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "hubspot_invoice_bridge.deal_flag_property",
                "cotizaciones_aprobadas",
            )
        )
        valor = (deal_props.get(propiedad) or "").strip()
        if not valor:
            return False
        if valor.lower() in ("false", "0", "no"):
            return False
        return True

    def _signed_quote(self, deal_id):
        """Primera cotización firmada del negocio, o ``False``.

        La firma no es una sola propiedad. Una cotización puede firmarse a
        mano —y entonces basta ``hs_manually_signed``— o electrónicamente, y
        en ese caso lo que decide es que hayan firmado todos los que tenían
        que firmar, no que el proceso esté abierto.
        """
        client = self.env["hubspot.client"]
        asociadas = client.get_associated_ids("deals", deal_id, "quotes")
        if not asociadas:
            return False

        quotes = client.batch_read(
            "quotes", [qid for qid, _p in asociadas], QUOTE_PROPERTIES
        )
        for quote in quotes:
            props = quote.get("properties") or {}
            if self._quote_is_signed(props):
                return str(quote.get("id") or "")
        return False

    @api.model
    def _quote_is_signed(self, props):
        if str(props.get("hs_manually_signed", "")).lower() == "true":
            return True
        if str(props.get("hs_esign_enabled", "")).lower() == "true":
            requeridos = self._to_float(props.get("hs_esign_num_signers_required"))
            completados = self._to_float(props.get("hs_esign_num_signers_completed"))
            if requeridos and completados >= requeridos:
                return True
        estados = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.signed_quote_statuses", "")
        )
        aceptados = {x.strip().upper() for x in estados.split(",") if x.strip()}
        if aceptados:
            for campo in ("hs_quote_esign_status", "hs_status"):
                if str(props.get(campo, "")).upper() in aceptados:
                    return True
        return False

    # ------------------------------------------------------------------
    # Construcción de la factura
    # ------------------------------------------------------------------

    def _create_document(self, deal_id, deal_props, quote_id=False):
        """Crea el documento que corresponde: orden de venta o factura.

        Una factura no mueve inventario. Si el negocio lleva equipos y se
        factura directo, el cliente se queda con un biométrico que para Odoo
        sigue en la bodega: sin salida, sin serial asignado y sin forma de
        rastrearlo cuando entre la garantía. El almacén se entera por el hueco
        en el estante.

        Por eso, cuando entre las líneas hay algún producto almacenable, lo que
        nace es una **orden de venta**. Al confirmarla Odoo genera la orden de
        entrega, y es al validarla cuando el equipo sale de existencias con su
        número de serie. La factura viene después, desde la misma orden, y así
        la venta, la salida y el cobro cuentan la misma historia.

        Los negocios de puro licenciamiento —que son la mayoría— siguen yendo
        directos a factura: no hay nada que descontar y pasar por una orden de
        venta solo añadiría un paso.
        """
        self.ensure_one()

        mapping = self._resolve_location(deal_props)
        partner = self._resolve_partner(deal_id)
        lines = self._build_invoice_lines(
            deal_id, mapping.company_id, quote_id, deal_props
        )
        currency = self._resolve_currency(deal_props, mapping)
        comercial = self._resolve_salesperson(deal_props)
        narration = self._prepare_narration(deal_props)
        nombre = deal_props.get("dealname") or deal_id

        if self._needs_warehouse(lines):
            documento = self._create_sale_order(
                deal_id,
                quote_id,
                mapping,
                partner,
                lines,
                currency,
                comercial,
                narration,
                nombre,
            )
            cuerpo = _(
                "Orden de venta generada automáticamente desde el negocio de "
                "HubSpot <b>%(name)s</b> (id %(id)s).<br/>"
                "Lleva producto de almacén, así que la salida de inventario se "
                "registra al confirmarla y entregarla; la factura se crea "
                "desde aquí.",
                name=nombre,
                id=deal_id,
            )
        else:
            documento = self._create_invoice(
                deal_id,
                quote_id,
                mapping,
                partner,
                lines,
                currency,
                comercial,
                narration,
                nombre,
            )
            cuerpo = _(
                "Factura generada automáticamente desde el negocio de HubSpot "
                "<b>%(name)s</b> (id %(id)s).",
                name=nombre,
                id=deal_id,
            )

        documento.message_post(body=cuerpo)
        self._writeback_to_hubspot(deal_id, documento)
        return documento

    def _needs_warehouse(self, lines):
        """¿Hay algo que descontar del almacén?

        Lo decide el producto, no el nombre ni la categoría: ``is_storable`` es
        exactamente la pregunta «¿Odoo lleva existencias de esto?». Los
        servicios y las licencias responden que no, así que un negocio de puro
        software no pasa por almacén.

        El modo se puede forzar en Ajustes para los portales que quieran
        siempre una cosa o siempre la otra.
        """
        modo = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.document_mode", "auto")
        )
        if modo == "invoice":
            return False
        if modo == "sale_order":
            return True

        productos = (
            self.env["product.product"]
            .sudo()
            .browse([l["product_id"] for l in lines if l.get("product_id")])
        )
        return any(productos.mapped("is_storable"))

    def _create_sale_order(
        self,
        deal_id,
        quote_id,
        mapping,
        partner,
        lines,
        currency,
        comercial,
        narration,
        nombre,
    ):
        """La orden de venta, en presupuesto. Nunca se confirma sola.

        Confirmarla reserva existencias y genera la entrega, y eso es una
        decisión de quien factura: puede que el equipo no esté, que haya que
        partir el envío o que el pedido cambie. Se deja en presupuesto, que es
        lo que se acordó en la reunión del 22 de septiembre.
        """
        vals = {
            "partner_id": partner.id,
            "company_id": mapping.company_id.id,
            # La moneda va por la tarifa, no por el campo. Ver _resolve_pricelist.
            "pricelist_id": self._resolve_pricelist(currency, mapping, partner).id,
            "origin": _("HubSpot %s", nombre),
            "client_order_ref": nombre,
            "hubspot_deal_id": deal_id,
            "hubspot_quote_id": quote_id or False,
            "hubspot_portal_id": self.portal_id or False,
            "order_line": [
                Command.create(
                    {
                        "product_id": line["product_id"],
                        "name": line["name"],
                        "product_uom_qty": line["quantity"],
                        "price_unit": line["price_unit"],
                        "discount": line["discount"],
                    }
                )
                for line in lines
            ],
        }
        fuente = self._hubspot_source()
        if fuente:
            vals["source_id"] = fuente.id
        if mapping.fiscal_position_id:
            vals["fiscal_position_id"] = mapping.fiscal_position_id.id
        if comercial:
            vals["user_id"] = comercial.id
        if narration:
            vals["note"] = narration
        return self.env["sale.order"].with_company(mapping.company_id).create(vals)

    def _hubspot_source(self):
        """El origen «By HubSpot» con el que se marca todo lo que entra.

        Se busca por su identificador externo y no por el nombre: si alguien
        lo renombra o lo traduce, los documentos nuevos siguen cayendo en el
        mismo registro y los informes no se parten en dos.

        Si el registro se borró, se devuelve vacío en lugar de fallar. Marcar
        el origen es útil, pero no es motivo para no crear el documento.
        """
        return self.env.ref(
            "hubspot_invoice_bridge.utm_source_hubspot", raise_if_not_found=False
        )

    def _create_invoice(
        self,
        deal_id,
        quote_id,
        mapping,
        partner,
        lines,
        currency,
        comercial,
        narration,
        nombre,
    ):
        """La factura, siempre en borrador.

        Nunca se valida ni se contabiliza: faltan datos que se revisan a mano
        —fecha, referencias fiscales, el cliente recién creado—.
        """
        move_vals = {
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "company_id": mapping.company_id.id,
            "journal_id": mapping.journal_id.id,
            "currency_id": currency.id,
            "invoice_origin": _("HubSpot %s", nombre),
            "ref": nombre,
            "hubspot_deal_id": deal_id,
            "hubspot_quote_id": quote_id or False,
            "hubspot_portal_id": self.portal_id or False,
            "invoice_line_ids": [Command.create(line) for line in lines],
        }
        fuente = self._hubspot_source()
        if fuente:
            move_vals["source_id"] = fuente.id
        if mapping.fiscal_position_id:
            move_vals["fiscal_position_id"] = mapping.fiscal_position_id.id
        if comercial:
            move_vals["invoice_user_id"] = comercial.id
        if narration:
            move_vals["narration"] = narration
        return (
            self.env["account.move"].with_company(mapping.company_id).create(move_vals)
        )

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

    def _resolve_pricelist(self, currency, mapping, partner):
        """La tarifa, que es lo que de verdad fija la moneda de un pedido.

        En ``sale.order`` la moneda no se escribe: se calcula a partir de la
        tarifa. Pasar ``currency_id`` en los valores no da error y tampoco
        hace nada, y el resultado es silencioso y equivocado: el pedido sale
        en la moneda de la tarifa con los precios que vinieron de HubSpot. Un
        negocio de 4.020,32 USD acababa siendo un pedido de 556,36 COP, con
        los precios unitarios en dólares puestos como si fueran pesos.

        Si no hay tarifa en la moneda del negocio se para. Un documento con
        el importe mal no se nota al crearlo: se nota al cobrarlo.
        """
        propia = partner.property_product_pricelist
        if propia and propia.currency_id == currency:
            return propia

        tarifa = (
            self.env["product.pricelist"]
            .with_company(mapping.company_id)
            .search(
                [
                    ("currency_id", "=", currency.id),
                    ("company_id", "in", (False, mapping.company_id.id)),
                ],
                limit=1,
            )
        )
        if tarifa:
            return tarifa

        raise UserError(
            _(
                "El negocio está en %(moneda)s y no hay ninguna tarifa en esa "
                "moneda para %(compania)s. Sin ella el pedido saldría con los "
                "precios de HubSpot pero en otra moneda, es decir con el "
                "importe mal y sin que nada lo avise.\n\n"
                "Cree una tarifa en %(moneda)s y reprocese el evento.",
                moneda=currency.name,
                compania=mapping.company_id.name,
            )
        )

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

        props = company.get("properties") or {}

        # Antes de crear, buscar al que ya existe. El cliente de un negocio
        # ganado casi nunca es nuevo: lleva años facturándose, con su NIT, su
        # dirección y su configuración fiscal. Crear otro deja una factura sin
        # identificación tributaria —que no se puede emitir— y parte el
        # histórico del cliente en dos fichas.
        partner = self._buscar_cliente_existente(props)
        if partner:
            partner.sudo().write({"hubspot_company_id": company_id})
            self._completar_huecos(partner, props)
            _logger.info(
                "La empresa %s de HubSpot se enlazó con el cliente que ya "
                "existía: %s (id %s).",
                company_id,
                partner.display_name,
                partner.id,
            )
            self._sync_primary_contact(deal_id, partner)
            return partner

        vals = self._prepare_partner_vals(company_id, props)
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

    @staticmethod
    def _normalizar_nit(texto):
        """Solo los dígitos. El NIT se escribe de mil maneras.

        900.628.818, 900628818-1 y 900628818 son el mismo número escrito por
        tres personas distintas.
        """
        return re.sub(r"\D", "", str(texto or ""))

    @staticmethod
    def _normalizar_nombre(texto):
        """Nombre de empresa comparable: sin puntuación, en mayúsculas.

        «ALISTAR S.A.S.» en HubSpot y «ALISTAR SAS» en Odoo son la misma
        empresa. Sin normalizar, la búsqueda por nombre no encontraría casi
        nada y se seguirían creando duplicados.
        """
        limpio = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+", " ", str(texto or ""))
        return re.sub(r"\s+", " ", limpio).strip().upper()

    def _buscar_cliente_existente(self, props):
        """El cliente que ya está en Odoo, si lo hay.

        El cliente de un negocio ganado casi nunca es nuevo: lleva tiempo
        facturándose, con su NIT y su configuración fiscal. Buscarlo antes de
        crear evita dos daños a la vez: una ficha duplicada y una factura sin
        identificación tributaria, que no se puede emitir.

        Dos vías, de más fiable a menos. Ante la duda no se enlaza nada: un
        cliente de más se corrige en un minuto, una factura emitida a quien no
        era, no.
        """
        Partner = self.env["res.partner"]
        vat_property = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.vat_property", "nit")
        )

        # 1. Por NIT, que es un identificador y no admite interpretación.
        crudo = str(props.get(vat_property) or "").strip()
        nit = self._normalizar_nit(crudo)
        if nit:
            candidatos = Partner.search(
                [
                    ("parent_id", "=", False),
                    "|",
                    ("vat", "in", [nit, crudo]),
                    ("vat", "ilike", nit),
                ],
                limit=20,
            )
            iguales = candidatos.filtered(
                lambda p: self._normalizar_nit(p.vat) == nit
            )
            if len(iguales) == 1:
                return iguales
            if len(iguales) > 1:
                _logger.warning(
                    "El NIT %s coincide con %s clientes de Odoo; no se enlaza "
                    "ninguno para no elegir mal.",
                    nit,
                    len(iguales),
                )
                return Partner.browse()

        # 2. Por nombre normalizado, y solo si la coincidencia es única.
        nombre = self._normalizar_nombre(props.get("name"))
        if not nombre:
            return Partner.browse()

        # Se filtra primero por la primera palabra para no recorrer la agenda
        # entera, y la comparación fina se hace ya sobre pocos registros.
        candidatos = Partner.search(
            [
                ("is_company", "=", True),
                ("parent_id", "=", False),
                ("name", "ilike", nombre.split(" ")[0]),
            ],
            limit=80,
        )
        iguales = candidatos.filtered(
            lambda p: self._normalizar_nombre(p.name) == nombre
        )
        if len(iguales) == 1:
            return iguales
        if len(iguales) > 1:
            _logger.warning(
                "El nombre '%s' coincide con %s clientes de Odoo; no se enlaza "
                "ninguno.",
                nombre,
                len(iguales),
            )
        return Partner.browse()

    def _completar_huecos(self, partner, props):
        """Rellena lo que al cliente le falte, sin pisar nada.

        Lo que ya está en Odoo lo puso contabilidad y vale más que lo que
        venga del CRM. Así que solo se escriben los campos vacíos.
        """
        vals = self._prepare_partner_vals(partner.hubspot_company_id, props)
        for campo in ("name", "is_company", "company_type", "hubspot_company_id"):
            vals.pop(campo, None)

        huecos = {c: v for c, v in vals.items() if v and not partner[c]}
        if not huecos:
            return

        try:
            with self.env.cr.savepoint():
                partner.sudo().write(huecos)
        except ValidationError:
            # Mismo criterio que al crear: un NIT con formato inesperado no
            # puede impedir que el resto de datos entre.
            rechazado = huecos.pop("vat", None)
            if not rechazado:
                raise
            if huecos:
                partner.sudo().write(huecos)
            _logger.warning(
                "El NIT '%s' de HubSpot no pasó la validación de Odoo para "
                "%s; se completó el resto.",
                rechazado,
                partner.display_name,
            )
            return

        _logger.info(
            "Completados %s del cliente %s desde HubSpot.",
            ", ".join(sorted(huecos)),
            partner.display_name,
        )

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

    # Formato del número de cotización de HubSpot: 20250902-164252135.
    # Ocho dígitos de fecha, guion y una marca de tiempo. Se extrae por patrón
    # y no partiendo por separadores porque el campo es texto libre: de los 312
    # negocios que lo tienen relleno, unos usan coma, uno «y», uno un salto de
    # línea, y 48 escriben alrededor —«N.° 20251008-145352484»,
    # «Referencia: 20250621-154325838»—. Buscar el patrón los recoge todos sin
    # tener que adivinar cómo lo escribió cada quien.
    QUOTE_NUMBER_RE = r"\d{6,}-\d{6,}"

    def _approved_quotes(self, deal_id, deal_props):
        """Las cotizaciones que el negocio declara aprobadas, resueltas a ids.

        El campo dice **cuáles** se aprobaron, y pueden ser varias. No es un sí
        o un no: es la lista de la que salen los productos.

        Devuelve ``(ids, perdidas, declara_algo)``:

        * ``ids`` — las cotizaciones del negocio que corresponden.
        * ``perdidas`` — números que parecen de cotización y no son de este
          negocio. Eso sí es un error: traer productos de otro cliente sería
          peor que no facturar.
        * ``declara_algo`` — el campo tiene contenido aunque no se haya podido
          sacar ningún número. Pasa cuando alguien escribe «Si» o el número de
          una factura de Odoo. No se inventa nada: se cae al negocio.

        Los números se casan contra las cotizaciones **asociadas a este
        negocio**, nunca contra todo el portal, así que un número repetido en
        otro negocio no puede colarse.
        """
        propiedad = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "hubspot_invoice_bridge.deal_flag_property", "cotizaciones_aprobadas"
            )
        )
        crudo = (deal_props.get(propiedad) or "").strip()
        if not crudo:
            return [], [], False

        numeros = []
        for n in re.findall(self.QUOTE_NUMBER_RE, crudo):
            if n not in numeros:
                numeros.append(n)
        if not numeros:
            return [], [], True

        client = self.env["hubspot.client"]
        asociadas = client.get_associated_ids("deals", deal_id, "quotes")
        if not asociadas:
            return [], numeros, True

        cotizaciones = client.batch_read(
            "quotes", [qid for qid, _primary in asociadas], QUOTE_PROPERTIES
        )
        indice = {}
        for q in cotizaciones:
            props = q.get("properties") or {}
            for clave in (q.get("id"), props.get("hs_quote_number")):
                if clave:
                    indice[str(clave).strip()] = q.get("id")

        encontradas, perdidas = [], []
        for numero in numeros:
            qid = indice.get(numero)
            if qid:
                if qid not in encontradas:
                    encontradas.append(qid)
            else:
                perdidas.append(numero)
        return encontradas, perdidas, True

    def _line_item_sources(self, deal_id, quote_id, deal_props):
        """De dónde salen los productos, en orden de preferencia.

        Puede ser más de un sitio: un negocio con dos cotizaciones aprobadas
        aporta los productos de las dos, y en Odoo se juntan en **una sola**
        cotización. Esa fue la aclaración del equipo comercial.
        """
        preferencia = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("hubspot_invoice_bridge.line_items_source", "auto")
        )
        if preferencia == "deal":
            return [("deals", deal_id)], []

        aprobadas, perdidas, declara_algo = self._approved_quotes(deal_id, deal_props)
        if perdidas:
            raise UserError(
                _(
                    "El negocio %(deal)s declara como aprobadas unas "
                    "cotizaciones que no están asociadas a él: %(perdidas)s.\n\n"
                    "Revise el campo de cotizaciones aprobadas en HubSpot: "
                    "debe nombrar cotizaciones del propio negocio, por su "
                    "número, su título o su identificador.",
                    deal=deal_id,
                    perdidas=", ".join(perdidas),
                )
            )

        if aprobadas:
            return [("quotes", q) for q in aprobadas], aprobadas

        # El campo dice algo pero sin número reconocible —«Si», el número de
        # una factura—. Se cae al negocio en lugar de fallar: es aprobación,
        # solo que sin indicar cuál.
        # Sin campo relleno: la que disparó, y si no el negocio.
        if preferencia == "quote":
            if not quote_id:
                raise UserError(
                    _(
                        "Los ajustes dicen que las líneas vienen de la "
                        "cotización, pero el negocio %s no declara ninguna "
                        "aprobada ni tiene una que haya disparado el proceso.",
                        deal_id,
                    )
                )
            return [("quotes", quote_id)], [quote_id]

        fuentes = ([("quotes", quote_id)] if quote_id else []) + [("deals", deal_id)]
        return fuentes, ([quote_id] if quote_id else [])

    def _build_invoice_lines(self, deal_id, company, quote_id=False, deal_props=None):
        """Las líneas, reunidas de todas las cotizaciones aprobadas.

        Un negocio puede declarar varias cotizaciones aprobadas, y en Odoo
        salen **en un solo documento**: el comercial no manda dos, manda una
        con todo. Por eso aquí se recorren todas las fuentes y se acumula, en
        lugar de parar en la primera que tenga productos.

        Los impuestos no se copian de HubSpot: los pone Odoo a partir del
        producto y la posición fiscal, que es lo único que garantiza que el IVA
        salga como manda la localización. Por eso la línea no lleva impuesto.
        """
        client = self.env["hubspot.client"]
        fuentes, _cotizaciones = self._line_item_sources(
            deal_id, quote_id, deal_props or {}
        )

        line_item_ids = []
        for tipo, objeto in fuentes:
            for lid, _primary in client.get_associated_ids(tipo, objeto, "line_items"):
                if lid not in line_item_ids:
                    line_item_ids.append(lid)
            # Si las fuentes son cotizaciones se suman todas. Si es el negocio,
            # es la única que hay.
            if line_item_ids and tipo == "deals":
                break

        if not line_item_ids:
            raise UserError(
                _(
                    "Ni el negocio %(deal)s ni sus cotizaciones aprobadas "
                    "tienen productos asociados en HubSpot, así que no hay "
                    "nada que cotizar.",
                    deal=deal_id,
                )
            )

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
            # Se aborta entero: un documento a medias es peor que ninguno.
            raise UserError(
                _(
                    "No se pudieron homologar estos productos de HubSpot con "
                    "productos de Odoo:\n\n%(items)s\n\n"
                    "Homologue los productos y reprocese el evento.",
                    items="\n".join("• %s" % u for u in unmatched),
                )
            )

        return lines

    def _resolve_salesperson(self, deal_props):
        """El propietario del negocio en HubSpot, como comercial de la factura.

        Se cruza por correo, que es el único dato que las dos plataformas
        comparten de verdad. Sin coincidencia se devuelve vacío y la factura
        sale sin comercial: es preferible a colgársela a quien no toca, sobre
        todo cuando de ese campo cuelgan las comisiones.
        """
        owner_id = deal_props.get("hubspot_owner_id")
        if not owner_id:
            return self.env["res.users"].browse()
        owner = self.env["hubspot.client"].get_owner(owner_id)
        email = (owner.get("email") or "").strip()
        if not email:
            return self.env["res.users"].browse()
        usuario = (
            self.env["res.users"].sudo().search([("login", "=ilike", email)], limit=1)
        )
        if not usuario:
            usuario = (
                self.env["res.users"]
                .sudo()
                .search([("email", "=ilike", email)], limit=1)
            )
        if not usuario:
            _logger.info(
                "hubspot: el propietario %s (%s) no tiene usuario en Odoo; la "
                "factura queda sin comercial.",
                owner_id,
                email,
            )
        return usuario

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

    def _writeback_to_hubspot(self, deal_id, documento):
        """Opcional: deja constancia en el negocio del documento creado.

        Sirve para los dos: lo que le interesa al comercial que mira HubSpot es
        el enlace, y le da igual que detrás haya una orden de venta o una
        factura.
        """
        param = self.env["ir.config_parameter"].sudo()
        property_name = param.get_param("hubspot_invoice_bridge.writeback_property", "")
        if not property_name:
            return
        try:
            base_url = param.get_param("web.base.url", "")
            accion = (
                "sale.action_quotations_with_onboarding"
                if documento._name == "sale.order"
                else "account.action_move_out_invoice_type"
            )
            value = (
                "%s/odoo/action-%s/%s" % (base_url.rstrip("/"), accion, documento.id)
                if base_url
                else str(documento.id)
            )
            self.env["hubspot.client"].update_deal_properties(
                deal_id, {property_name: value}
            )
        except Exception:  # noqa: BLE001 - accesorio
            _logger.exception(
                "No se pudo escribir la referencia del documento en el negocio %s",
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
