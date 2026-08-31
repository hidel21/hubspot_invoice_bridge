from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    hubspot_deal_id = fields.Char(
        string="Negocio de HubSpot",
        index=True,
        copy=False,
        readonly=True,
        help="Identificador del negocio de HubSpot que originó esta factura. "
        "Garantiza que un mismo negocio no se facture dos veces.",
    )

    hubspot_quote_id = fields.Char(
        string="Cotización de HubSpot",
        index=True,
        copy=False,
        readonly=True,
        help="Cotización que disparó esta factura, cuando el proceso arranca "
        "en la firma y no en la etapa del negocio. Vacío si facturó el "
        "negocio directamente.",
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
            "Ya existe una factura generada para ese negocio de HubSpot.",
        ),
    ]

    @api.depends("hubspot_deal_id", "hubspot_portal_id")
    def _compute_hubspot_deal_url(self):
        for move in self:
            if move.hubspot_deal_id and move.hubspot_portal_id:
                move.hubspot_deal_url = (
                    "https://app.hubspot.com/contacts/%s/deal/%s"
                    % (move.hubspot_portal_id, move.hubspot_deal_id)
                )
            else:
                move.hubspot_deal_url = False

    def action_open_hubspot_deal(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": self.hubspot_deal_url,
            "target": "new",
        }
