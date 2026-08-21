from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HubspotLocationMapping(models.Model):
    """Traduce la propiedad de localización del negocio en HubSpot al destino
    contable de Odoo: compañía, diario de venta y posición fiscal.

    El nombre técnico de la propiedad que se lee del negocio se configura en
    Ajustes (``hubspot_invoice_bridge.location_property``). El valor recibido
    se busca aquí; si el negocio no trae valor, o el valor no está mapeado, se
    usa la línea marcada como predeterminada.
    """

    _name = "hubspot.location.mapping"
    _description = "Mapeo de localización HubSpot → Odoo"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(string="Nombre", required=True)
    hubspot_value = fields.Char(
        string="Valor en HubSpot",
        help="Valor interno de la propiedad de localización del negocio. "
        "Déjelo vacío únicamente en la línea predeterminada.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Compañía",
        required=True,
        default=lambda self: self.env.company,
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Diario de venta",
        required=True,
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
    )
    fiscal_position_id = fields.Many2one(
        "account.fiscal.position",
        string="Posición fiscal",
        domain="[('company_id', '=', company_id)]",
        help="Opcional. Si se deja vacío, Odoo aplica la posición fiscal "
        "que corresponda al cliente.",
    )
    currency_id = fields.Many2one(
        "res.currency",
        string="Moneda por defecto",
        help="Se usa cuando el negocio de HubSpot no indica moneda o ésta no "
        "existe en Odoo. Si se deja vacío, se usa la de la compañía.",
    )
    is_default = fields.Boolean(
        string="Predeterminada",
        help="Línea usada cuando el negocio no trae localización o su valor "
        "no está mapeado.",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "hubspot_value_uniq",
            "unique(hubspot_value)",
            "Ya existe un mapeo para ese valor de localización de HubSpot.",
        ),
    ]

    @api.constrains("journal_id", "company_id", "fiscal_position_id")
    def _check_company_consistency(self):
        for mapping in self:
            if mapping.journal_id.company_id != mapping.company_id:
                raise ValidationError(
                    _(
                        "El diario '%(journal)s' no pertenece a la compañía "
                        "'%(company)s'.",
                        journal=mapping.journal_id.display_name,
                        company=mapping.company_id.display_name,
                    )
                )
            fpos = mapping.fiscal_position_id
            if fpos and fpos.company_id != mapping.company_id:
                raise ValidationError(
                    _(
                        "La posición fiscal '%(fpos)s' no pertenece a la compañía "
                        "'%(company)s'.",
                        fpos=fpos.display_name,
                        company=mapping.company_id.display_name,
                    )
                )

    @api.constrains("is_default", "active")
    def _check_single_default(self):
        for mapping in self:
            if not (mapping.is_default and mapping.active):
                continue
            other = self.search(
                [
                    ("is_default", "=", True),
                    ("id", "!=", mapping.id),
                ],
                limit=1,
            )
            if other:
                raise ValidationError(
                    _(
                        "Sólo puede haber un mapeo de localización predeterminado. "
                        "Ya lo es '%s'.",
                        other.name,
                    )
                )

    @api.model
    def resolve(self, hubspot_value):
        """Devuelve el mapeo aplicable, o un recordset vacío si no hay ninguno."""
        if hubspot_value:
            mapping = self.search([("hubspot_value", "=", str(hubspot_value))], limit=1)
            if mapping:
                return mapping
        return self.search([("is_default", "=", True)], limit=1)
