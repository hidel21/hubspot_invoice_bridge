"""Auditoría del puente HubSpot: recorre el camino completo sin credenciales.

Sustituye el cliente de la API por respuestas fijas, de modo que se pueda
ejercitar de verdad la creación de la factura —que es donde se dice que está el
fallo de invoice_line_ids— sin tocar el portal ni necesitar token.
Todo dentro de una transacción que se deshace al final.
"""

RESULTADOS = []


def comprobar(nombre, condicion, detalle=''):
    RESULTADOS.append((nombre, bool(condicion)))
    print('  %s %s%s' % ('[ok]  ' if condicion else '[FALLA]', nombre,
                         '  →  %s' % detalle if detalle else ''))


def titulo(t):
    print('\n' + t); print('-' * len(t))


# ───────────────────────── datos de partida ──────────────────────────
compania = env['res.company'].browse(13)
diario = env['account.journal'].search(
    [('company_id', '=', compania.id), ('type', '=', 'sale')], limit=1)
prod_sku = env['product.product'].browse(130)      # MIT, se busca por SKU
prod_nombre = env['product.product'].browse(131)   # MIA, se busca por nombre

print('=' * 72)
print('AUDITORÍA DEL PUENTE HUBSPOT')
print('=' * 72)
print('compañía %s | diario %s | productos %s y %s'
      % (compania.name, diario.name, prod_sku.default_code, prod_nombre.default_code))

# ────────────────── respuestas simuladas de HubSpot ──────────────────
NEGOCIO = {
    'id': '987654321',
    'properties': {
        'dealname': 'Renovación licencias 2026',
        'amount': '1200000',
        'dealstage': 'closedwon',
        'pipeline': 'default',
        'deal_currency_code': 'COP',
        'closedate': '2026-08-20T00:00:00Z',
        'categoria_de_negocio': 'Software',
        'cotizaciones_aprobadas': 'COT-4471',
        'licencia_elastica': 'true',
        'modalidad_licencia_anual': 'true',
    },
}
EMPRESA = {
    'id': '555000111',
    'properties': {
        'name': 'AUDITORÍA HUBSPOT SAS',
        'domain': 'auditoria.test',
        'address': 'Calle 100 # 20-30', 'city': 'Bogotá', 'zip': '110111',
        'country': 'Colombia', 'phone': '+57 1 5551234',
        'nit': '900123456-7',
        'correo_de_facturacion': 'facturacion@auditoria.test',
    },
}
CONTACTO = {
    'id': '777888',
    'properties': {'firstname': 'Ana', 'lastname': 'Pruebas',
                   'email': 'ana@auditoria.test', 'phone': '3001234567',
                   'jobtitle': 'Compras'},
}
# Dos ítems: uno se homologa por SKU y otro por nombre exacto. El primero trae
# descuento como IMPORTE (semántica estándar de HubSpot), el segundo no trae.
ITEMS = [
    {'id': '1', 'properties': {
        'name': 'Licencia MyIntelli Time', 'hs_sku': prod_sku.default_code,
        'hs_product_id': '9001', 'quantity': '10', 'price': '10000',
        'discount': '20000'}},
    {'id': '2', 'properties': {
        'name': prod_nombre.name, 'hs_sku': '', 'hs_product_id': '9002',
        'quantity': '5', 'price': '2000'}},
]

Cliente = type(env['hubspot.client'])
_original = {n: getattr(Cliente, n) for n in
             ('get_object', 'batch_read', 'get_associated_ids',
              'get_deal_pipelines', 'update_deal_properties')}
LLAMADAS = []


def _get_object(self, object_type, object_id, properties=None, with_history=None):
    LLAMADAS.append(('get_object', object_type, str(object_id)))
    return {'deals': NEGOCIO, 'companies': EMPRESA, 'contacts': CONTACTO}.get(object_type)


def _batch_read(self, object_type, object_ids, properties):
    LLAMADAS.append(('batch_read', object_type, list(object_ids)))
    return ITEMS


def _get_associated_ids(self, from_type, from_id, to_type):
    LLAMADAS.append(('assoc', from_type, to_type))
    return {'companies': [(EMPRESA['id'], True)],
            'contacts': [(CONTACTO['id'], True)],
            'line_items': [('1', True), ('2', False)]}.get(to_type, [])


# Dos pipelines como los del portal: el de ventas y el de JoobPay, cada uno con
# su etapa ganada identificada por metadatos y no por el nombre.
PIPELINES = [
    {'id': 'default', 'label': 'Pipeline de ventas', 'stages': [
        {'id': 'appointmentscheduled', 'label': 'Cita programada', 'displayOrder': 0,
         'metadata': {'isClosed': 'false', 'probability': '0.2'}},
        {'id': 'closedwon', 'label': 'Cerrado ganado', 'displayOrder': 1,
         'metadata': {'isClosed': 'true', 'probability': '1.0'}},
        {'id': 'closedlost', 'label': 'Cerrado perdido', 'displayOrder': 2,
         'metadata': {'isClosed': 'true', 'probability': '0.0'}}]},
    {'id': '793842865', 'label': 'Pipeline de ventas - JoobPay', 'stages': [
        {'id': '1163347550', 'label': 'Cerrado ganado', 'displayOrder': 5,
         'metadata': {'isClosed': 'true', 'probability': '1.0'}}]},
]


def _get_deal_pipelines(self):
    LLAMADAS.append(('pipelines',))
    return PIPELINES


def _sin_api(self, *a, **k):
    raise AssertionError('no debería llamarse')


Cliente.get_object = _get_object
Cliente.batch_read = _batch_read
Cliente.get_associated_ids = _get_associated_ids
Cliente.get_deal_pipelines = _get_deal_pipelines
Cliente.update_deal_properties = _sin_api

# ─────────────────────── configuración mínima ────────────────────────
titulo('1. Configuración necesaria para que el puente funcione')
mapeo = env['hubspot.location.mapping'].create({
    'name': 'Auditoría', 'hubspot_value': 'default',
    'company_id': compania.id, 'journal_id': diario.id, 'is_default': True})
etapa = env['hubspot.deal.stage'].create({
    'stage_id': 'closedwon', 'label': 'Cerrado ganado',
    'pipeline_id': 'default', 'trigger_invoice': True})
comprobar('se puede crear el mapeo de localización', bool(mapeo))
comprobar('se puede dar de alta la etapa a mano, sin API', bool(etapa))
comprobar('la etapa dispara facturación',
          env['hubspot.deal.stage'].stage_triggers_invoice('closedwon'))
# Una etapa desconocida provoca una sincronización completa contra la API. Es
# comportamiento buscado —para no descartar en silencio una etapa nueva— pero
# significa que un evento cualquiera puede acabar llamando a HubSpot.
antes = len(LLAMADAS)
dispara = env['hubspot.deal.stage'].stage_triggers_invoice('appointmentscheduled')
comprobar('una etapa que no es ganada no dispara factura', not dispara)
comprobar('pero consultarla provocó una llamada a la API',
          ('pipelines',) in LLAMADAS[antes:], 'sincroniza los pipelines')
comprobar('la sincronización detecta las etapas ganadas por metadatos',
          env['hubspot.deal.stage'].search_count([('is_won', '=', True)]) == 2,
          'closedwon y la de JoobPay')
comprobar('no marca como ganada la etapa perdida',
          not env['hubspot.deal.stage'].search(
              [('stage_id', '=', 'closedlost')]).trigger_invoice)

# ───────────────────────── ingesta del webhook ───────────────────────
titulo('2. Ingesta del evento')
carga = [{'eventId': 900001, 'subscriptionType': 'deal.propertyChange',
          'objectId': 987654321, 'propertyName': 'dealstage',
          'propertyValue': 'closedwon', 'occurredAt': 1786924800000,
          'portalId': 21830287}]
Evento = env['hubspot.webhook.event']
creados = Evento.ingest_batch(carga)
comprobar('el evento se encola', len(creados) == 1)
repetido = Evento.ingest_batch(carga)
comprobar('el mismo eventId no se duplica', len(repetido) == 0)
comprobar('guarda el payload íntegro', bool(creados.payload and 'eventId' in creados.payload))

# ──────────────────────── el camino completo ─────────────────────────
titulo('3. Procesar el evento: aquí se crea la factura')
resultado = creados._process_one()
comprobar('el evento se procesa sin excepción', resultado is True,
          'estado %s' % creados.state)
if creados.error_message:
    print('       detalle: %s' % creados.error_message[:200])

factura = creados.move_id
comprobar('se creó la factura', bool(factura), 'id %s' % (factura.id or '-'))

if factura:
    titulo('4. La factura creada')
    comprobar('queda en borrador', factura.state == 'draft', factura.state)
    comprobar('es factura de cliente', factura.move_type == 'out_invoice')
    comprobar('compañía y diario los del mapeo',
              factura.company_id == compania and factura.journal_id == diario)
    comprobar('el cliente es la EMPRESA, no el contacto',
              factura.partner_id.is_company and factura.partner_id.hubspot_company_id == EMPRESA['id'],
              factura.partner_id.name)
    comprobar('guarda el id del negocio para no duplicar',
              factura.hubspot_deal_id == '987654321')
    comprobar('el NIT llegó al cliente', factura.partner_id.vat,
              factura.partner_id.vat or 'vacío')
    comprobar('el contacto se creó como hijo',
              env['res.partner'].search_count(
                  [('hubspot_contact_id', '=', CONTACTO['id'])]) == 1)
    comprobar('la nota interna trae el contexto comercial',
              factura.narration and 'COT-4471' in factura.narration)

    lineas = factura.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
    comprobar('tiene las dos líneas', len(lineas) == 2, '%s líneas' % len(lineas))
    porsku = lineas.filtered(lambda l: l.product_id == prod_sku)
    pornombre = lineas.filtered(lambda l: l.product_id == prod_nombre)
    comprobar('homologa por SKU', bool(porsku))
    comprobar('homologa por nombre exacto', bool(pornombre))
    if porsku:
        # descuento de 20.000 sobre 10 x 10.000 = 100.000 → 20 %
        comprobar('convierte el descuento de importe a porcentaje',
                  abs(porsku.discount - 20.0) < 0.01, '%.2f %%' % porsku.discount)
        # La comparación lleva tolerancia a propósito: en esta base la
        # precisión decimal de las unidades está en 15 dígitos, y redondear a
        # 15 dígitos sobre un float reintroduce error de representación. Por
        # eso una cantidad de 10 se guarda como 10.000000000000009. No lo causa
        # el puente —pasa igual creando la línea a mano— pero una comparación
        # exacta aquí acusaría al inocente.
        comprobar('la cantidad es la de HubSpot',
                  abs(porsku.quantity - 10) < 1e-6, repr(porsku.quantity))
        comprobar('el precio es el de HubSpot, no el de la tarifa de Odoo',
                  abs(porsku.price_unit - 10000) < 0.01, repr(porsku.price_unit))
        comprobar('el subtotal descuenta',
                  abs(porsku.price_subtotal - 80000) < 1, porsku.price_subtotal)
    comprobar('Odoo puso los impuestos, no HubSpot',
              bool(lineas.mapped('tax_ids')),
              'impuestos: %s' % ', '.join(lineas.mapped('tax_ids.name')[:2]) or 'ninguno')
    comprobar('recuerda el producto de HubSpot para la próxima',
              prod_sku.hubspot_product_id == '9001',
              prod_sku.hubspot_product_id or 'no guardado')

    titulo('5. No se factura dos veces el mismo negocio')
    otro = Evento.ingest_batch([dict(carga[0], eventId=900002)])
    otro._process_one()
    comprobar('el segundo evento reconoce la factura existente',
              otro.state == 'done' and otro.move_id == factura,
              otro.error_message or '')
    comprobar('no se creó una factura nueva',
              env['account.move'].search_count(
                  [('hubspot_deal_id', '=', '987654321')]) == 1)

# ───────────────────── el descuento como porcentaje ──────────────────
titulo('6. La duda del descuento: la otra interpretación')
env['ir.config_parameter'].sudo().set_param(
    'hubspot_invoice_bridge.discount_is_percentage', 'True')
ITEMS[0]['properties']['discount'] = '15'
ev = Evento.ingest_batch([dict(carga[0], eventId=900003, objectId=111222333)])
NEGOCIO['id'] = '111222333'
ev._process_one()
if ev.move_id:
    l = ev.move_id.invoice_line_ids.filtered(lambda x: x.product_id == prod_sku)
    comprobar('con la casilla marcada, 15 se lee como 15 %',
              abs(l.discount - 15.0) < 0.01, '%.2f %%' % l.discount)
else:
    comprobar('se pudo crear la segunda factura', False, ev.error_message or '')

# ──────────────────────── errores controlados ────────────────────────
titulo('7. Qué pasa cuando falta algo')
Cliente.get_associated_ids = lambda self, f, i, t: [] if t == 'companies' else [('1', True)]
ev2 = Evento.ingest_batch([dict(carga[0], eventId=900004, objectId=444555666)])
NEGOCIO['id'] = '444555666'
ev2._process_one()
comprobar('negocio sin empresa asociada queda en error',
          ev2.state == 'error' and 'empresa' in (ev2.error_message or '').lower(),
          (ev2.error_message or '')[:80])
comprobar('cuenta el intento para no reintentar sin fin', ev2.attempts == 1)

Cliente.get_associated_ids = _get_associated_ids
ITEMS.append({'id': '3', 'properties': {'name': 'PRODUCTO QUE NO EXISTE EN ODOO',
                                        'hs_sku': 'NO-EXISTE-999', 'quantity': '1',
                                        'price': '1000'}})
ev3 = Evento.ingest_batch([dict(carga[0], eventId=900005, objectId=777888999)])
NEGOCIO['id'] = '777888999'
ev3._process_one()
comprobar('un producto sin homologar aborta la factura entera',
          ev3.state == 'error' and not ev3.move_id,
          (ev3.error_message or '')[:90])
comprobar('el error nombra el producto y su SKU',
          'NO-EXISTE-999' in (ev3.error_message or ''))

# ──────────────────────────── cierre ─────────────────────────────────
for nombre, funcion in _original.items():
    setattr(Cliente, nombre, funcion)
env.cr.rollback()

print('\n' + '=' * 72)
fallos = [n for n, ok in RESULTADOS if not ok]
print('RESUMEN: %s comprobaciones, %s correctas, %s con fallo'
      % (len(RESULTADOS), len(RESULTADOS) - len(fallos), len(fallos)))
for n in fallos:
    print('  FALLA: %s' % n)
print('Llamadas a la API simuladas: %s' % len(LLAMADAS))
print('La transacción se deshizo: la base queda como estaba.')
print('=' * 72)
