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
