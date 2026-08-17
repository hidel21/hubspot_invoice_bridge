from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    hubspot_product_id = fields.Char(
        string='ID de producto en HubSpot', index=True, copy=False,
        help="Identificador del producto en el catálogo de HubSpot "
             "(propiedad 'hs_product_id' de los ítems de línea). Se rellena "
             "solo la primera vez que un producto se homologa por SKU o "
             "nombre, para que las siguientes búsquedas sean directas.")

    _sql_constraints = [
        ('hubspot_product_uniq', 'unique(hubspot_product_id)',
         'Ya existe un producto vinculado a ese producto de HubSpot.'),
    ]
