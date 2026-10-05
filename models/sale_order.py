from odoo import api, fields, models


class SaleOrder(models.Model):
    """La orden de venta que nace de un negocio ganado con hardware.

    Una factura no mueve inventario. Si el negocio lleva equipos, facturar
    directamente los saca de la contabilidad sin sacarlos del almacén: el
    cliente se queda con un biométrico que para Odoo sigue en la bodega, sin
    serial asignado y sin forma de rastrearlo cuando entre la garantía.

    La orden de venta sí lo mueve. Al confirmarla Odoo genera la orden de
    entrega, y es al validarla cuando el equipo sale de existencias con su
    número de serie. La factura viene después, desde la misma orden.
    """

    _inherit = "sale.order"

    hubspot_deal_id = fields.Char(
        string="Negocio de HubSpot",
        index=True,
        copy=False,
        readonly=True,
        help="Identificador del negocio de HubSpot que originó esta orden. "
        "Garantiza que un mismo negocio no entre dos veces.",
    )
    hubspot_quote_id = fields.Char(
        string="Cotización de HubSpot",
        index=True,
        copy=False,
        readonly=True,
    )
    hubspot_portal_id = fields.Char(
        string="Portal de HubSpot", copy=False, readonly=True
    )
    hubspot_deal_url = fields.Char(
        string="Enlace al negocio", compute="_compute_hubspot_deal_url"
    )

    _sql_constraints = [
        (
            "hubspot_deal_uniq",
            "unique(hubspot_deal_id)",
            "Ya existe una orden de venta generada para ese negocio de HubSpot.",
        ),
    ]

    @api.depends("hubspot_deal_id", "hubspot_portal_id")
    def _compute_hubspot_deal_url(self):
        for order in self:
            if order.hubspot_deal_id and order.hubspot_portal_id:
                order.hubspot_deal_url = (
                    "https://app.hubspot.com/contacts/%s/deal/%s"
                    % (order.hubspot_portal_id, order.hubspot_deal_id)
                )
            else:
                order.hubspot_deal_url = False

    def action_open_hubspot_deal(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": self.hubspot_deal_url,
            "target": "new",
        }
