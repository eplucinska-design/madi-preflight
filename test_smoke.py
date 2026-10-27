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
        cls.server = subprocess.Popen([sys.executable, 'server.py', '18765'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            with self.post('/api/sample', {'page': 1, 'x': x * mm, 'y': y * mm}, buf.getvalue()) as response:
                return json.load(response)['plates']
        background, knockout, overprint = plates(100, 90), plates(40, 60), plates(150, 60)
        self.assertAlmostEqual(background['C'], 20, delta=1)
        self.assertEqual(background['HOT'], 0)
        self.assertEqual((knockout['C'], knockout['M']), (0, 0))
        self.assertAlmostEqual(knockout['HOT'], 50, delta=1)
        self.assertAlmostEqual(overprint['M'], 40, delta=1)
        self.assertAlmostEqual(overprint['HOT'], 100, delta=1)

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
