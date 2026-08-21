from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    hubspot_company_id = fields.Char(
        string="ID de empresa en HubSpot",
        index=True,
        copy=False,
        help="Identificador del objeto 'company' de HubSpot. Es la clave de "
        "homologación del cliente a facturar.",
    )
    hubspot_contact_id = fields.Char(
        string="ID de contacto en HubSpot",
        index=True,
        copy=False,
        help="Identificador del objeto 'contact' de HubSpot.",
    )

    _sql_constraints = [
        (
            "hubspot_company_uniq",
            "unique(hubspot_company_id)",
            "Ya existe un contacto vinculado a esa empresa de HubSpot.",
        ),
        (
            "hubspot_contact_uniq",
            "unique(hubspot_contact_id)",
            "Ya existe un contacto vinculado a ese contacto de HubSpot.",
        ),
    ]
