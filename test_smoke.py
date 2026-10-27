"""Synthetic PDF integration checks; no customer files required."""
import io
import json
import base64
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import fitz
import pikepdf


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([sys.executable, 'server.py', '18765'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=os.environ | {'MADI_RATE_PER_MINUTE': '10000'})
        for _ in range(40):
            try:
                urllib.request.urlopen('http://127.0.0.1:18765/').close()
                break
            except OSError:
                time.sleep(.1)
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(255, 142))
        page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(HS=pikepdf.Array([
            pikepdf.Name.Separation, pikepdf.Name.HS, pikepdf.Name.DeviceCMYK,
            pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[0, 1, 0, 0], N=1)])))
        page.Resources.ExtGState = pikepdf.Dictionary(Production=pikepdf.Dictionary(OP=True, op=False, OPM=1))
        page.Contents = pdf.make_stream(b'q 0.1 0.1 0.1 1 k 10 10 30 30 re f Q q 0 0 0 0 k 50 10 30 30 re f Q q /HS cs 1 scn 90 10 30 30 re f Q q /HS CS 1 SCN 10 80 m 220 80 l S Q')
        buf = io.BytesIO()
        pdf.save(buf)
        cls.pdf = buf.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait()

    def post(self, path, fields, data=None):
        boundary = 'madi-smoke'
        body = b''
        for key, value in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="test.pdf"\r\n\r\n'.encode()
        body += (self.pdf if data is None else data) + f'\r\n--{boundary}--\r\n'.encode()
        return urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18765' + path, data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}))

    def test_operations_and_previews(self):
        for mode in ['bleed', 'black', 'white', 'matrix', 'shape', 'remove', 'dieline', 'imposer', 'cleanup']:
            with self.subTest(mode=mode):
                with self.post('/api/process', {'mode': mode, 'spot': 'HS', 'bleed': '3',
                        'cut_enabled': 'true', 'cut_mode': 'spot', 'cut_value': 'HS'}) as response:
                    result = response.read()
                with fitz.open(stream=result, filetype='pdf') as doc:
                    self.assertGreater(len(doc), 0)
                    if mode == 'imposer':
                        self.assertAlmostEqual(doc[0].rect.width * 25.4 / 72, 320, places=1)
                with self.post('/api/analyze', {}, result) as response:
                    self.assertTrue(json.load(response)['preview'])

    def test_analyze_reports_page_boxes_in_displayed_orientation(self):
        source = fitz.open()
        page = source.new_page(width=200, height=100)
        page.set_trimbox(fitz.Rect(10, 5, 180, 90))
        page.set_rotation(90)
        payload = source.tobytes()
        source.close()
        with self.post('/api/analyze', {}, payload) as response:
            boxes = json.load(response)['boxes']
        mm = 25.4 / 72
        for actual, expected in zip(boxes['trimbox'], (10, 10, 95, 180)):
            self.assertAlmostEqual(actual, expected * mm, places=2)
        self.assertAlmostEqual(boxes['mediabox'][3], 200 * mm, places=2)

    def spots(self, data=None):
        with self.post('/api/spots', {}, data) as response:
            return {item['name']: item for item in json.load(response)['spots']}

    def process(self, step):
        with self.post('/api/process', {'workflow': json.dumps([step])}) as response:
            return response.read()

    def test_spot_report_counts_fill_and_stroke(self):
        hs = self.spots()['HS']
        self.assertEqual((hs['fill'], hs['stroke'], hs['overprint_fill']), (1, 1, 0))
        self.assertEqual(hs['alternate'], 'DeviceCMYK')

    def test_spot_rename_and_overprint_in_exported_file(self):
        renamed = self.spots(self.process({'mode': 'spot_rename', 'spot_name': 'HS', 'spot_new_name': 'HOT'}))
        self.assertEqual(list(renamed), ['HOT'])
        result = self.spots(self.process({'mode': 'spot_overprint', 'spot_name': 'HS', 'overprint_target': 'fill', 'overprint_value': True}))
        self.assertEqual((result['HS']['overprint_fill'], result['HS']['overprint_stroke']), (1, 0))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.process({'mode': 'spot_rename', 'spot_name': 'Missing', 'spot_new_name': 'X'})
        self.assertEqual(error.exception.code, 422)

    def test_spot_extract_keeps_only_spot_objects(self):
        output = self.process({'mode': 'spot_extract', 'spot_name': 'HS', 'spot_placement': 'after'})
        with fitz.open(stream=output, filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)
            plate = doc[1].get_pixmap(alpha=False)
            original = doc[0].get_pixmap(alpha=False)
            y = plate.height - 25  # squares are drawn at y 10..40 in PDF space
            self.assertNotEqual(original.pixel(25, y), (255, 255, 255))   # CMYK square on the original
            self.assertEqual(plate.pixel(25, y), (255, 255, 255))         # removed from the plate
            self.assertNotEqual(plate.pixel(105, y), (255, 255, 255))     # HS square kept
        self.assertIn('HS', self.spots(output))

    def test_separation_sampler_simulates_knockout_and_overprint(self):
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(200, 100))
        page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(HS=pikepdf.Array([
            pikepdf.Name.Separation, pikepdf.Name('/HOT'), pikepdf.Name.DeviceCMYK,
            pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[0, .3, 1, 0], N=1)])),
            ExtGState=pikepdf.Dictionary(OP=pikepdf.Dictionary(op=True, OP=True, OPM=1)))
        page.Contents = pdf.make_stream(b'0.2 0.4 0 0.1 k 0 0 200 100 re f q /HS cs 0.5 scn 10 10 60 60 re f Q '
                                        b'q /OP gs /HS cs 1 scn 120 10 60 60 re f Q')
        buf = io.BytesIO()
        pdf.save(buf)
        mm = 25.4 / 72
        def plates(x, y):
            try:
                with self.post('/api/sample', {'page': 1, 'x': x * mm, 'y': y * mm}, buf.getvalue()) as response:
                    return json.load(response)['plates']
            except urllib.error.HTTPError as error:
                self.fail('Sampler error: ' + error.read().decode('utf-8', 'replace'))
        background, knockout, overprint = plates(100, 90), plates(40, 60), plates(150, 60)
        self.assertAlmostEqual(background['C'], 20, delta=1)
        self.assertEqual(background['HOT'], 0)
        self.assertEqual((knockout['C'], knockout['M']), (0, 0))
        self.assertAlmostEqual(knockout['HOT'], 50, delta=1)
        self.assertAlmostEqual(overprint['M'], 40, delta=1)
        self.assertAlmostEqual(overprint['HOT'], 100, delta=1)

    def test_preflight_report_levels_and_locations(self):
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(300, 200))
        page.TrimBox = [8.5, 8.5, 291.5, 191.5]
        page.BleedBox = [5, 5, 295, 195]   # ~1.2 mm bleed
        image = pikepdf.Stream(pdf, b'\xff\x00\x00' * 64)
        image.Type, image.Subtype, image.Width, image.Height = pikepdf.Name.XObject, pikepdf.Name.Image, 8, 8
        image.ColorSpace, image.BitsPerComponent = pikepdf.Name.DeviceRGB, 8
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im=image),
            ExtGState=pikepdf.Dictionary(OP=pikepdf.Dictionary(op=True, OP=True)),
            Font=pikepdf.Dictionary(F=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name('/ArialMT'))))
        page.Contents = pdf.make_stream(b'q 100 0 0 100 20 20 cm /Im Do Q q /OP gs 0 0 0 0 k 150 20 50 50 re f Q '
                                        b'q 1 0 0 rg 220 20 30 30 re f Q BT /F 12 Tf 20 150 Td (Tekst) Tj ET')
        buf = io.BytesIO()
        pdf.save(buf)
        with self.post('/api/preflight', {'bleed': 3, 'dpi_error': 150, 'dpi_warning': 250}, buf.getvalue()) as response:
            result = json.load(response)
        codes = {(item['level'], item['code']) for item in result['issues']}
        for expected in [('warning', 'bleed'), ('error', 'image_dpi'), ('warning', 'image_rgb'),
                         ('error', 'white_overprint'), ('warning', 'vector_rgb'), ('error', 'font_not_embedded')]:
            self.assertIn(expected, codes)
        image_issue = next(item for item in result['issues'] if item['code'] == 'image_dpi')
        self.assertEqual(image_issue['page'], 1)
        self.assertAlmostEqual(image_issue['bbox_mm'][0], 20 * 25.4 / 72, places=1)
        self.assertGreater(result['counts']['error'], 0)
        self.assertIn('nie zastepuje', result['limitations'])

    def test_trim_imposition_matches_montax_reference_layout(self):
        # Reference: Montax N-UP two-sided mirrored, 2 pages 210x198 trim + 3 mm bleed, sheet 488x330,
        # 1x2 positions, gap 6 mm, margins 8 mm: page 1 front-left, page 2 on the back behind it.
        mm = 72 / 25.4
        source = fitz.open()
        for number in (1, 2):
            page = source.new_page(width=216 * mm, height=204 * mm)
            page.set_trimbox(fitz.Rect(3 * mm, 3 * mm, 213 * mm, 201 * mm))
            page.draw_rect(page.rect, color=None, fill=(1, 0, 0))
            page.insert_text((40 * mm, 100 * mm), f'PAGE-{number}', fontsize=40)
        payload = source.tobytes()
        source.close()
        step = {'mode': 'imposer', 'imposer_sheet': 'custom', 'imposer_sheet_width_mm': 488, 'imposer_sheet_height_mm': 330,
            'imposer_rows': 1, 'imposer_columns': 2, 'imposer_gap_x_mm': 6, 'imposer_gap_y_mm': 6, 'imposer_repeat_count': 1,
            'imposer_margin_top_mm': 8, 'imposer_margin_bottom_mm': 8, 'imposer_margin_left_mm': 8, 'imposer_margin_right_mm': 8,
            'imposer_duplex': True, 'imposer_duplex_pairing': 'alternate', 'imposer_mirror_back': True,
            'imposer_fit_mode': 'trim', 'imposer_bleed_mm': 3, 'imposer_crop_marks': True, 'imposer_draw_sheet_labels': False}
        with self.post('/api/process', {'workflow': json.dumps([step])}, payload) as response:
            with fitz.open(stream=response.read(), filetype='pdf') as doc:
                self.assertEqual(len(doc), 2)
                front = {w[4]: w[0] / mm for w in doc[0].get_text('words')}
                back = {w[4]: w[0] / mm for w in doc[1].get_text('words')}
                self.assertAlmostEqual(front['PAGE-1'], 31 + 37, delta=0.5)   # trim starts at 31 mm, 100 % scale
                self.assertAlmostEqual(back['PAGE-2'], 247 + 37, delta=0.5)   # behind page 1 on the mirrored back
                self.assertNotIn('PAGE-2', front)
                ink = doc[0].get_pixmap(matrix=fitz.Matrix(1 / mm, 1 / mm), alpha=False)
                row = [x for x in range(ink.width) if ink.pixel(x, 165) != (255, 255, 255)]
                self.assertAlmostEqual(row[0], 28, delta=1)    # 3 mm bleed outside the trim
                self.assertAlmostEqual(row[-1], 244, delta=1)  # inner bleed limited to half the 6 mm gap

    def test_set_page_boxes_in_displayed_orientation(self):
        source = fitz.open()
        source.new_page(width=300, height=200)
        rotated = source.new_page(width=300, height=200)
        rotated.set_rotation(90)
        payload = source.tobytes()
        source.close()
        step = {'mode': 'boxes', 'boxes_target': 'trim', 'boxes_reference': 'media', 'boxes_top_mm': 5,
                'boxes_bottom_mm': 0, 'boxes_left_mm': 0, 'boxes_right_mm': 0, 'boxes_pages': ''}
        with self.post('/api/process', {'workflow': json.dumps([step])}, payload) as response:
            result = response.read()
        for page in (1, 2):
            with self.post('/api/analyze', {'page': page}, result) as response:
                boxes = json.load(response)['boxes']
            self.assertAlmostEqual(boxes['trimbox'][1], 5, places=2)          # top edge as displayed
            self.assertAlmostEqual(boxes['trimbox'][0], 0, places=2)
            self.assertAlmostEqual(boxes['trimbox'][3], boxes['mediabox'][3], places=2)
        with fitz.open(stream=result, filetype='pdf') as doc:
            self.assertEqual(doc[0].get_contents(), fitz.open(stream=payload, filetype='pdf')[0].get_contents())
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/process', {'workflow': json.dumps([{**step, 'boxes_top_mm': 400}])}, payload)

    def test_colour_conversions_on_explicit_command(self):
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(200, 100))
        page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(HS=pikepdf.Array([
            pikepdf.Name.Separation, pikepdf.Name('/HOT'), pikepdf.Name.DeviceCMYK,
            pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[0, .3, 1, 0], N=1)])),
            ExtGState=pikepdf.Dictionary(OP=pikepdf.Dictionary(op=True, OP=True)))
        page.Contents = pdf.make_stream(b'q /HS cs 0.5 scn 10 10 30 30 re f Q q 0.1 0.9 0.2 0 k 50 10 30 30 re f Q '
                                        b'q /OP gs 0 0 0 0 k 90 10 30 30 re f Q')
        buf = io.BytesIO()
        pdf.save(buf)
        data = buf.getvalue()
        def run(step):
            with self.post('/api/process', {'workflow': json.dumps([step])}, data) as response:
                return response.read()
        converted = run({'mode': 'spot_to_cmyk', 'spot_name': 'HOT'})
        self.assertNotIn('HOT', {k for k, v in self.spots(converted).items() if v['fill']})
        with pikepdf.open(io.BytesIO(converted)) as result:
            self.assertIn(b'0 0.15 0.5 0 k', result.pages[0].Contents.read_bytes())
        spotted = self.spots(run({'mode': 'cmyk_to_spot', 'source_cmyk': '10,90,20,0', 'spot_new_name': 'LAKIER'}))
        self.assertEqual(spotted['LAKIER']['fill'], 1)
        with pikepdf.open(io.BytesIO(run({'mode': 'spot_alternate', 'spot_name': 'HOT', 'spot_cmyk': '10,20,30,0'}))) as result:
            self.assertEqual([float(v) for v in result.pages[0].Resources.ColorSpace.HS[3].C1], [.1, .2, .3, 0])
        with self.post('/api/preflight', {}, run({'mode': 'white_overprint_fix'})) as response:
            self.assertNotIn('white_overprint', {item['code'] for item in json.load(response)['issues']})

    def test_page_operations_delete_insert_merge_split(self):
        import zipfile
        payload = self.numbered_pdf(5, trim=(5, 5, 95, 135))
        def run(step, data=payload):
            with self.post('/api/process', {'workflow': json.dumps([step])}, data) as response:
                return response.read()
        with fitz.open(stream=run({'mode': 'delete_pages', 'delete_range': '2,4-5'}), filetype='pdf') as doc:
            self.assertEqual([page.get_text().strip() for page in doc], ['PAGE-1', 'PAGE-3'])
        with fitz.open(stream=run({'mode': 'insert_blank', 'blank_position': 'after', 'blank_page': 1, 'blank_count': 2}), filetype='pdf') as doc:
            self.assertEqual(len(doc), 7)
            self.assertEqual(doc[1].get_text(), '')
            self.assertEqual(doc[1].trimbox, doc[0].trimbox)       # blank page sized like its neighbour
            self.assertIn('PAGE-2', doc[3].get_text())
        with self.assertRaises(urllib.error.HTTPError):
            run({'mode': 'delete_pages', 'delete_range': '1-5'})
        other = self.numbered_pdf(2)
        body = b''
        for key, data in (('file', payload), ('file_1', other)):
            body += f'--x\r\nContent-Disposition: form-data; name="{key}"; filename="{key}.pdf"\r\n\r\n'.encode() + data + b'\r\n'
        body += b'--x--\r\n'
        request = urllib.request.Request('http://127.0.0.1:18765/api/merge', data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=x'})
        with urllib.request.urlopen(request) as response, fitz.open(stream=response.read(), filetype='pdf') as doc:
            self.assertEqual(len(doc), 7)
            self.assertEqual(doc[0].trimbox, fitz.Rect(5, 5, 95, 135))
        with self.post('/api/split', {'split_mode': 'every', 'split_value': 2, 'name': 'test'}, payload) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        self.assertEqual(sorted(archive.namelist()), ['test_01_str1-2.pdf', 'test_02_str3-4.pdf', 'test_03_str5.pdf'])
        with fitz.open(stream=archive.read('test_03_str5.pdf'), filetype='pdf') as doc:
            self.assertIn('PAGE-5', doc[0].get_text())

    def test_thin_lines_report_and_stroke_width_fix(self):
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(200, 200))
        page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(CC=pikepdf.Array([
            pikepdf.Name.Separation, pikepdf.Name('/CutContour'), pikepdf.Name.DeviceCMYK,
            pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[0, 1, 0, 0], N=1)])))
        # 1 pt line drawn at 10 % scale = 0.1 pt (0.035 mm); a 0-width hairline; a normal 1 pt line; a cut line.
        page.Contents = pdf.make_stream(b'q 0.1 0 0 0.1 0 0 cm 0 0 0 1 K 1 w 100 100 m 1000 100 l S Q '
                                        b'0 0 0 1 K 0 w 10 50 m 190 50 l S 1 w 10 80 m 190 80 l S '
                                        b'q /CC CS 1 SCN 2 w 10 150 m 190 150 l S Q')
        buf = io.BytesIO()
        pdf.save(buf)
        data = buf.getvalue()
        with self.post('/api/preflight', {'min_line_mm': 0.1}, data) as response:
            thin = [i for i in json.load(response)['issues'] if i['code'] == 'thin_lines']
        self.assertEqual(len(thin), 1)
        self.assertIn(': 2 ', thin[0]['message'])   # scaled line and hairline; the 1 pt and 2 pt lines pass
        with self.post('/api/process', {'workflow': json.dumps([{'mode': 'stroke_width', 'stroke_mode': 'min', 'stroke_width_mm': 0.1}])}, data) as response:
            fixed = response.read()
        with self.post('/api/preflight', {'min_line_mm': 0.1}, fixed) as response:
            self.assertNotIn('thin_lines', {i['code'] for i in json.load(response)['issues']})
        with self.post('/api/process', {'workflow': json.dumps([{'mode': 'stroke_width', 'stroke_mode': 'spot', 'stroke_width_mm': 0.0882, 'spot_name': 'CutContour'}])}, data) as response:
            cut = response.read()
        with fitz.open(stream=cut, filetype='pdf') as doc:
            widths = sorted(round(d['width'], 2) for d in doc[0].get_drawings() if d.get('width'))
        self.assertIn(0.25, widths)   # cut line now 0.25 pt; other lines untouched
        self.assertIn(1.0, widths)

    def test_imposition_registration_marks_and_sheet_description(self):
        step = {'mode': 'imposer', 'imposer_sheet': 'SRA3', 'imposer_rows': 1, 'imposer_columns': 1, 'imposer_repeat_count': 1,
                'imposer_fit_mode': 'trim', 'imposer_registration_marks': True, 'imposer_draw_sheet_labels': True,
                'imposer_job_name': 'zlecenie_42.pdf'}
        with self.post('/api/process', {'workflow': json.dumps([step])}, self.numbered_pdf(1, trim=(5, 5, 95, 135))) as response:
            with fitz.open(stream=response.read(), filetype='pdf') as doc:
                text = doc[0].get_text()
                circles = [d for d in doc[0].get_drawings() if any(item[0] == 'c' for item in d['items'])]
        self.assertIn('zlecenie_42.pdf', text)
        self.assertIn('arkusz 1/1', text)
        self.assertEqual(len(circles), 4)   # one registration target per side

    def test_cleanup_handles_zero_size_paths(self):
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(200, 200))
        page.Contents = pdf.make_stream(b'0 0 0 1 K 0.25 w 10 10 m 10 10 l S 5 100 m 15 100 l S q 0 1 0 0 k 50 50 100 100 re f Q')
        buf = io.BytesIO()
        pdf.save(buf)
        with self.post('/api/process', {'mode': 'cleanup'}, buf.getvalue()) as response:
            with fitz.open(stream=response.read(), filetype='pdf') as doc:
                self.assertEqual(len(doc), 1)
                self.assertTrue(any(d.get('fill') for d in doc[0].get_drawings()))

    def test_invalid_input(self):
        for fields, data in [({}, b'bad'), ({'page': '999'}, self.pdf)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/analyze', fields, data)
            self.assertEqual(error.exception.code, 422)

    def test_workflow_order_and_page_organization(self):
        steps = [{'mode': 'bleed', 'page_size': 'custom', 'page_width_mm': 90,
                  'page_height_mm': 50, 'bleed': 0, 'bleed_mode': 'none'},
                 {'mode': 'pages', 'page_range': '1,1', 'rotation': 90}]
        with self.post('/api/process', {'workflow': json.dumps(steps)}) as response:
            with fitz.open(stream=response.read(), filetype='pdf') as doc:
                self.assertEqual(len(doc), 2)
                self.assertAlmostEqual(doc[0].rect.width * 25.4 / 72, 50, places=1)
                self.assertAlmostEqual(doc[0].rect.height * 25.4 / 72, 90, places=1)

    def test_inspector_reports_resource_not_object_overprint(self):
        with self.post('/api/inspect', {}) as response:
            report = json.load(response)
        self.assertIn('HS', report['spots'])
        self.assertGreater(report['vector_count'], 0)
        self.assertTrue(report['overprint_resources'][0]['stroke'])
        self.assertFalse(report['overprint_resources'][0]['fill'])
        self.assertIn('nie potwierdzaja', report['limitations'])

    def test_bad_workflow_and_extreme_range(self):
        for steps in [[], [{'mode': 'bleed'}]*13, [{'mode': 'pages', 'page_range': '1-999999999'}],
                      [{'mode': 'bleed', 'bleed': 'NaN'}], [{'mode': 'unknown'}]]:
            with self.subTest(steps=steps), self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/process', {'workflow': json.dumps(steps)})
            self.assertEqual(error.exception.code, 422)

    def test_imposition_preview_matches_export_and_mirrors_back(self):
        source = fitz.open()
        for number in range(1, 5):
            source.new_page(width=100, height=140).insert_text((10, 30), f'PAGE-{number}')
        payload = source.tobytes()
        source.close()
        settings = {'mode': 'imposer', 'imposer_rows': 1, 'imposer_columns': 2,
            'imposer_repeat_count': 1, 'imposer_duplex': True, 'imposer_mirror_back': True,
            'imposer_draw_sheet_labels': False}
        with self.post('/api/process', {'workflow': json.dumps([settings])}, payload) as response:
            exported = response.read()
        with self.post('/api/process', {'workflow': json.dumps([{**settings, 'imposer_preview_sheet': 0}])}, payload) as response:
            preview = response.read()
        with fitz.open(stream=exported, filetype='pdf') as out, fitz.open(stream=preview, filetype='pdf') as plan:
            self.assertEqual(len(out), 2)
            self.assertEqual(len(plan), 2)
            for index in range(2):
                self.assertEqual(out[index].get_pixmap().samples, plan[index].get_pixmap().samples)
            # Default keeps the earlier sequential variant: front 1,2 and mirrored back 3,4.
            words = {word[4]: word for word in out[1].get_text('words')}
            self.assertGreater(words['PAGE-3'][0], words['PAGE-4'][0])
        sheets = self.sheet_words({**settings, 'imposer_duplex_pairing': 'alternate'}, payload)
        self.assertEqual([w for _, w in sheets[0]], ['PAGE-1', 'PAGE-3'])
        self.assertEqual([w for _, w in sheets[1]], ['PAGE-4', 'PAGE-2'])

    def numbered_pdf(self, count, width=100, height=140, trim=None):
        source = fitz.open()
        for number in range(1, count + 1):
            page = source.new_page(width=width, height=height)
            if trim:
                page.set_trimbox(fitz.Rect(trim))
            page.insert_text((10, 30), f'PAGE-{number}')
        payload = source.tobytes()
        source.close()
        return payload

    def sheet_words(self, settings, payload):
        with self.post('/api/process', {'workflow': json.dumps([settings])}, payload) as response:
            with fitz.open(stream=response.read(), filetype='pdf') as doc:
                return [sorted((w[0], w[4]) for w in page.get_text('words')) for page in doc]

    def test_imposition_orders(self):
        base = {'mode': 'imposer', 'imposer_rows': 1, 'imposer_columns': 2,
            'imposer_draw_sheet_labels': False}
        sheets = self.sheet_words({**base, 'imposer_repeat_count': 1, 'imposer_order': 'cut_stack'}, self.numbered_pdf(6))
        self.assertEqual([[w for _, w in s] for s in sheets], [['PAGE-1', 'PAGE-4'], ['PAGE-2', 'PAGE-5'], ['PAGE-3', 'PAGE-6']])
        sheets = self.sheet_words({**base, 'imposer_columns': 4, 'imposer_repeat_count': 2, 'imposer_order': 'collate'}, self.numbered_pdf(2))
        self.assertEqual([w for _, w in sheets[0]], ['PAGE-1', 'PAGE-2', 'PAGE-1', 'PAGE-2'])
        sheets = self.sheet_words({**base, 'imposer_columns': 4, 'imposer_repeat_count': 2}, self.numbered_pdf(2))
        self.assertEqual([w for _, w in sheets[0]], ['PAGE-1', 'PAGE-1', 'PAGE-2', 'PAGE-2'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/process', {'workflow': json.dumps([{**base, 'imposer_order': 'booklet'}])})
        self.assertEqual(error.exception.code, 422)

    def test_imposition_preview_matches_export_for_all_options(self):
        payload = self.numbered_pdf(5, 120, 80, trim=(6, 4, 112, 74))
        settings = {'mode': 'imposer', 'imposer_rows': 2, 'imposer_columns': 2, 'imposer_repeat_count': 1,
            'imposer_order': 'cut_stack', 'imposer_duplex': True, 'imposer_rotation': '90',
            'imposer_fit_mode': 'actual', 'imposer_scale_percent': 90, 'imposer_align': 'top-left',
            'imposer_crop_marks': True, 'imposer_add_blanks': False, 'imposer_draw_sheet_labels': False}
        with self.post('/api/process', {'workflow': json.dumps([settings])}, payload) as response:
            exported = response.read()
        with self.post('/api/process', {'workflow': json.dumps([{**settings, 'imposer_preview_sheet': 0}])}, payload) as response:
            preview = response.read()
        with fitz.open(stream=exported, filetype='pdf') as out, fitz.open(stream=preview, filetype='pdf') as plan:
            self.assertEqual(len(plan), 2)
            for index in range(2):
                self.assertEqual(out[index].get_pixmap().samples, plan[index].get_pixmap().samples)

    def test_actual_size_overflow_is_rejected(self):
        settings = {'mode': 'imposer', 'imposer_sheet': 'A4', 'imposer_rows': 1, 'imposer_columns': 1,
            'imposer_repeat_count': 1, 'imposer_fit_mode': 'actual', 'imposer_scale_percent': 1000}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/process', {'workflow': json.dumps([settings])})
        self.assertEqual(error.exception.code, 422)

    def test_crop_marks_follow_trim_box_for_every_rotation(self):
        sys.path.insert(0, 'engine')
        from types import SimpleNamespace
        from imposer import settings_from_args, placement, mm_to_pt
        source = fitz.open()
        page = source.new_page(width=120, height=80)
        trim = fitz.Rect(4, 8, 110, 70)
        page.set_trimbox(trim)
        page.draw_rect(trim, color=None, fill=(0, 0, 0))
        for rotation in ['0', '90', '180', '270']:
            with self.subTest(rotation=rotation):
                config = settings_from_args(SimpleNamespace(imposer_sheet='A4', imposer_rotation=rotation,
                    imposer_fit_mode='actual'))
                cell = fitz.Rect(20, 30, 20 + mm_to_pt(100), 30 + mm_to_pt(100))
                target, rotate, trim_rect = placement(config, page, cell)
                sheet = fitz.open()
                surface = sheet.new_page(width=600, height=800)
                surface.show_pdf_page(target, source, 0, keep_proportion=False, rotate=rotate)
                ink = fitz.Rect()
                for drawing in surface.get_drawings():
                    ink |= drawing['rect']
                for actual, expected in zip(ink, trim_rect):
                    self.assertAlmostEqual(actual, expected, delta=0.5)
                sheet.close()
        source.close()

    def test_blank_imposition_positions_have_no_production_ink(self):
        settings = {'mode': 'imposer', 'imposer_rows': 1, 'imposer_columns': 2,
            'imposer_repeat_count': 1, 'imposer_duplex': True,
            'imposer_add_blanks': True, 'imposer_draw_sheet_labels': False}
        with self.post('/api/process', {'workflow': json.dumps([settings])}) as response:
            output = response.read()
        with fitz.open(stream=output, filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)
            self.assertEqual(doc[1].get_text(), '')
            self.assertEqual(doc[1].get_drawings(), [])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/process', {'workflow': json.dumps([{**settings, 'imposer_preview_sheet': 1}])})
        self.assertEqual(error.exception.code, 422)

    def test_crop_marks_are_independent_of_frames(self):
        base = {'mode': 'imposer', 'imposer_rows': 1, 'imposer_columns': 2, 'imposer_repeat_count': 1,
            'imposer_margin_left_mm': 10, 'imposer_margin_right_mm': 10, 'imposer_margin_top_mm': 10,
            'imposer_margin_bottom_mm': 10, 'imposer_draw_sheet_labels': False, 'imposer_add_blanks': False}
        def lines(extra):
            with self.post('/api/process', {'workflow': json.dumps([{**base, **extra}])}) as response:
                output = response.read()
            with fitz.open(stream=output, filetype='pdf') as doc:
                return len(doc[0].get_drawings())
        plain = lines({})
        self.assertGreater(lines({'imposer_crop_marks': True}), plain)
        self.assertGreater(lines({'imposer_draw_frames': True}), plain)
        self.assertGreater(lines({'imposer_crop_marks': True}), lines({'imposer_draw_frames': True}))

    def test_health_and_security_headers(self):
        with urllib.request.urlopen('http://127.0.0.1:18765/healthz') as response:
            self.assertEqual(json.load(response)['status'], 'ok')
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_rate_limit_and_network_mode_guard(self):
        process = subprocess.Popen([sys.executable, 'server.py', '18767'], env=os.environ | {'MADI_RATE_PER_MINUTE': '3'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen('http://127.0.0.1:18767/healthz').close()
                    break
                except OSError:
                    time.sleep(.1)
            codes = []
            for _ in range(5):
                request = urllib.request.Request('http://127.0.0.1:18767/api/analyze', data=b'x',
                    headers={'Content-Type': 'text/plain'})
                try:
                    urllib.request.urlopen(request)
                except urllib.error.HTTPError as error:
                    codes.append(error.code)
            self.assertEqual(codes[:3], [422, 422, 422])
            self.assertEqual(codes[3:], [429, 429])
        finally:
            process.terminate()
            process.wait()
        env = {k: v for k, v in os.environ.items() if k not in ('MADI_PASSWORD', 'MADI_PUBLIC')}
        guarded = subprocess.run([sys.executable, 'server.py', '18768'], env=env | {'MADI_BIND': '0.0.0.0'},
            capture_output=True, timeout=30)
        self.assertNotEqual(guarded.returncode, 0)
        self.assertIn(b'MADI_PUBLIC', guarded.stderr)

    def test_server_password_and_workspace_assets(self):
        password = 'test-only-password'
        process = subprocess.Popen([sys.executable, 'server.py', '18766'],
            env=os.environ | {'MADI_PASSWORD': password}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen('http://127.0.0.1:18766/')
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 401)
                    break
                except OSError:
                    time.sleep(.1)
            headers = {'Authorization': 'Basic ' + base64.b64encode(('madi:' + password).encode()).decode()}
            for path in ['/', '/workspace.js', '/tools.js', '/workspace.css', '/vendor/lucide.min.js']:
                with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18766' + path, headers=headers)) as response:
                    self.assertEqual(response.status, 200)
                    self.assertGreater(len(response.read()), 100)
        finally:
            process.terminate()
            process.wait()


if __name__ == '__main__':
    unittest.main()
