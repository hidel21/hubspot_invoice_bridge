import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class HubspotDealStage(models.Model):
    """Espejo local de las etapas de los pipelines de negocios de HubSpot.

    Existe porque el webhook entrega el *id interno* de la etapa
    (``propertyValue``) y no su etiqueta. Comparar contra la cadena literal
    ``closedwon`` sólo funciona en el pipeline por defecto: en cualquier
    pipeline personalizado la etapa ganada tiene un id numérico propio.

    La marca de "ganada" se toma de HubSpot: ``metadata.isClosed`` a verdadero
    junto con ``metadata.probability`` igual a 1. El campo ``trigger_invoice``
    queda editable para poder ajustar manualmente qué etapas disparan la
    facturación sin depender de esa heurística.
    """

    _name = "hubspot.deal.stage"
    _description = "Etapa de negocio de HubSpot"
    _order = "pipeline_label, display_order, label"

    stage_id = fields.Char(
        string="ID de etapa",
        required=True,
        index=True,
        help="Identificador interno de la etapa en HubSpot; es el valor que "
        "llega en 'propertyValue' del webhook.",
    )
    label = fields.Char(string="Etiqueta", required=True)
    pipeline_id = fields.Char(string="ID de pipeline", required=True)
    pipeline_label = fields.Char(string="Pipeline")
    display_order = fields.Integer(string="Orden", default=0)
    probability = fields.Float(string="Probabilidad", digits=(3, 2))
    is_closed = fields.Boolean(string="Cerrada")
    is_won = fields.Boolean(
        string="Ganada (según HubSpot)",
        readonly=True,
        help="Calculado en la sincronización: etapa cerrada con probabilidad 1.",
    )
    trigger_invoice = fields.Boolean(
        string="Genera factura",
        help="Si está marcado, un negocio que entre en esta etapa genera una "
        "factura en borrador. Se propone automáticamente para las etapas "
        "ganadas, pero puede ajustarse a mano.",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "stage_uniq",
            "unique(stage_id)",
            "Ya existe una etapa de HubSpot con ese identificador.",
        ),
    ]

    def _compute_display_name(self):
        for stage in self:
            if stage.pipeline_label:
                stage.display_name = "%s / %s" % (stage.pipeline_label, stage.label)
            else:
                stage.display_name = stage.label or stage.stage_id

    # ------------------------------------------------------------------

    @api.model
    def action_sync_from_hubspot(self):
        """Trae los pipelines de negocios y refresca las etapas locales."""
        pipelines = self.env["hubspot.client"].get_deal_pipelines()
        seen = set()
        created = updated = 0

        for pipeline in pipelines:
            pipeline_id = str(pipeline.get("id") or "")
            pipeline_label = pipeline.get("label") or ""
            for stage in pipeline.get("stages") or []:
                stage_id = str(stage.get("id") or "")
                if not stage_id:
                    continue
                seen.add(stage_id)
                metadata = stage.get("metadata") or {}
                # HubSpot devuelve estos metadatos como cadenas.
                is_closed = str(metadata.get("isClosed", "")).lower() == "true"
                try:
                    probability = float(metadata.get("probability") or 0.0)
                except (TypeError, ValueError):
                    probability = 0.0
                is_won = is_closed and probability >= 1.0

                vals = {
                    "stage_id": stage_id,
                    "label": stage.get("label") or stage_id,
                    "pipeline_id": pipeline_id,
                    "pipeline_label": pipeline_label,
                    "display_order": stage.get("displayOrder") or 0,
                    "probability": probability,
                    "is_closed": is_closed,
                    "is_won": is_won,
                    "active": True,
                }

                existing = self.with_context(active_test=False).search(
                    [("stage_id", "=", stage_id)], limit=1
                )
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    # Sólo en la creación se propone 'trigger_invoice', para no
                    # pisar ajustes manuales en sincronizaciones posteriores.
                    vals["trigger_invoice"] = is_won
                    self.create(vals)
                    created += 1

        # Las etapas que ya no existen en HubSpot se archivan en vez de
        # borrarse: pueden estar referenciadas por eventos históricos.
        obsolete = self.search([("stage_id", "not in", list(seen))])
        if obsolete:
            obsolete.write({"active": False})

        _logger.info(
            "Sincronización de etapas HubSpot: %s creadas, %s actualizadas, "
            "%s archivadas",
            created,
            updated,
            len(obsolete),
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Etapas sincronizadas"),
                "message": _(
                    "%(created)s creadas, %(updated)s actualizadas, "
                    "%(archived)s archivadas.",
                    created=created,
                    updated=updated,
                    archived=len(obsolete),
                ),
                "sticky": False,
            },
        }

    @api.model
    def _cron_sync_stages(self):
        self.action_sync_from_hubspot()

    @api.model
    def stage_triggers_invoice(self, stage_id):
        """¿Entrar en esta etapa debe generar una factura?"""
        if not stage_id:
            return False
        stage = self.with_context(active_test=False).search(
            [("stage_id", "=", str(stage_id))], limit=1
        )
        if not stage:
            # Etapa desconocida: se sincroniza una vez por si es nueva en el
            # portal, en lugar de descartar el evento en silencio.
            _logger.info(
                "Etapa de HubSpot '%s' desconocida, sincronizando pipelines.", stage_id
            )
            self.action_sync_from_hubspot()
            stage = self.with_context(active_test=False).search(
                [("stage_id", "=", str(stage_id))], limit=1
            )
        return bool(stage and stage.trigger_invoice)
