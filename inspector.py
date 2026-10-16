"""Page inspection: resource declarations and rendered vector geometry."""
import io
import pikepdf


def inspect_page(data, doc, page_number):
    page = doc[page_number - 1]
    spots, spaces, states = set(), set(), []
    visited = set()

    def colors(value):
        if isinstance(value, pikepdf.Array):
            kind = str(value[0]) if len(value) else ''
            spaces.add(kind)
            if kind == '/Separation':
                spots.add(str(value[1]).lstrip('/'))
            elif kind == '/DeviceN':
                spots.update(str(item).lstrip('/') for item in value[1])
        else:
            spaces.add(str(value))

    def resources(res, scope='Page', depth=0):
        if depth > 20:
            return
        for value in res.get('/ColorSpace', {}).values():
            colors(value)
        for name, value in res.get('/ExtGState', {}).items():
            if '/OP' in value or '/op' in value or '/OPM' in value:
                states.append({'resource': scope + ':' + str(name),
                    'stroke': bool(value['/OP']) if '/OP' in value else None,
                    'fill': bool(value['/op']) if '/op' in value else None,
                    'mode': int(value['/OPM']) if '/OPM' in value else None})
        for name, obj in res.get('/XObject', {}).items():
            identity = obj.objgen
            if identity in visited:
                continue
            visited.add(identity)
            if '/ColorSpace' in obj:
                colors(obj['/ColorSpace'])
            if '/Resources' in obj:
                resources(obj['/Resources'], scope + ':' + str(name), depth + 1)

    with pikepdf.open(io.BytesIO(data)) as pdf:
        resources(pdf.pages[page_number - 1].Resources)
    fonts = []
    for font in page.get_fonts(full=True):
        xref, ext, kind, name = font[:4]
        embedded = bool(doc.extract_font(xref)[3]) if xref else False
        fonts.append({'name': name, 'type': kind, 'embedded': embedded})
    images = []
    for image in page.get_image_info():
        a, b, c, d, _, _ = image['transform']
        width, height = (a*a + b*b)**.5, (c*c + d*d)**.5
        images.append({'width': image['width'], 'height': image['height'],
            'dpi_x': round(image['width'] * 72 / width) if width else None,
            'dpi_y': round(image['height'] * 72 / height) if height else None,
            'colorspace': image.get('cs-name', '')})
    vectors = []
    drawings = page.get_drawings()
    for drawing in drawings[:2000]:
        rect = drawing['rect'] * page.rotation_matrix
        vectors.append({'id': len(vectors) + 1, 'bbox': list(rect),
            'fill_rgb': drawing.get('fill'), 'stroke_rgb': drawing.get('color'),
            'width_pt': drawing.get('width'), 'type': drawing.get('type')})
    boxes = {name: list(getattr(page, name)) for name in ('mediabox', 'cropbox', 'trimbox', 'bleedbox')}
    return {'spots': sorted(spots), 'spaces': sorted(spaces), 'overprint_resources': states,
        'fonts': fonts, 'images': images, 'vectors': vectors, 'vector_count': len(drawings),
        'boxes_pt': boxes, 'page_rect': list(page.rect),
        'limitations': 'Kolory geometrii sa wartosciami RGB podgladu. Zasoby overprint nie potwierdzaja nadruku konkretnego obiektu. Inspekcja nie jest certyfikacja PDF/X.'}
