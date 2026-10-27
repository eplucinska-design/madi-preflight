"""Run an ordered workflow in one isolated, time-limited child process."""
import argparse
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import fitz
import foil_preflight as engine
from imposer import process_imposer_pdf, settings_from_args, render_sheets
from cleanup_marks import process_cleanup_marks_pdf
import colors


def truth(value):
    return value is True or str(value).lower() == 'true'


def arguments(fields, src, out):
    parser = engine.build_parser()
    argv = [str(src), str(out)]
    fields = dict(fields)
    fields['bleed_mm'] = fields.get('bleed', 3)
    selector = fields.get('color_mode', 'spot')
    for key in ('spot', 'cmyk', 'rgb', 'gray'):
        if key != selector:
            fields.pop(key, None)
    for action in parser._actions:
        key = action.dest
        if not action.option_strings or key in {'help', 'mode', 'verbose'} or key not in fields:
            continue
        value = fields[key]
        if isinstance(action, argparse._StoreTrueAction):
            if truth(value):
                argv.append(action.option_strings[0])
        elif value != '' and value is not None:
            if action.type in (int, float):
                number = float(str(value).replace(',', '.'))
                if not math.isfinite(number) or abs(number) > 10000:
                    raise ValueError('Wartosc liczbowa poza zakresem: ' + key)
                value = str(int(number)) if action.type is int else str(number)
            argv.append(action.option_strings[0] + '=' + str(value))
    return parser.parse_args(argv)


def organize(fields, src, out):
    with fitz.open(src) as doc, fitz.open() as result:
        selection = []
        for part in str(fields.get('page_range', '')).split(','):
            values = part.strip().split('-')
            if len(values) == 1:
                selection.append(int(values[0]))
            elif len(values) == 2:
                start, end = map(int, values)
                if not 1 <= start <= len(doc) or not 1 <= end <= len(doc) or abs(end-start) >= 2000:
                    raise ValueError('Nieprawidlowy zakres stron.')
                selection.extend(range(start, end + (1 if end >= start else -1), 1 if end >= start else -1))
            else:
                raise ValueError('Nieprawidlowy zakres stron.')
            if len(selection) > 2000:
                raise ValueError('Limit wynosi 2000 stron wynikowych.')
        if not selection or any(number < 1 or number > len(doc) for number in selection):
            raise ValueError('Zakres wykracza poza dokument.')
        rotation = int(fields.get('rotation', 0))
        if rotation not in (0, 90, 180, 270):
            raise ValueError('Nieprawidlowy obrot.')
        for number in selection:
            result.insert_pdf(doc, from_page=number - 1, to_page=number - 1)
            result[-1].set_rotation((result[-1].rotation + rotation) % 360)
        result.save(out, garbage=4, deflate=True)


def run(step, src, out):
    mode = step.get('mode', 'bleed')
    if mode == 'pages':
        return organize(step, src, out)
    if mode == 'cleanup':
        return process_cleanup_marks_pdf(SimpleNamespace(input=src, output=out, **{
            key: value for key, value in step.items() if key.startswith('cleanup_')}))
    if mode == 'spot_rename':
        return colors.rename_spot(src, out, str(step.get('spot_name', '')), str(step.get('spot_new_name', '')))
    if mode == 'spot_extract':
        return colors.extract_spot(src, out, str(step.get('spot_name', '')).strip(), str(step.get('spot_placement', 'after')))
    if mode == 'spot_overprint':
        return colors.set_overprint(src, out, str(step.get('spot_name', '')).strip(),
            str(step.get('overprint_target', 'fill')), truth(step.get('overprint_value', True)))
    if mode == 'imposer':
        settings = {key: value for key, value in step.items() if key.startswith('imposer_')}
        for key, lo, hi, default in [('imposer_rows', 1, 20, 2), ('imposer_columns', 1, 20, 2),
                ('imposer_repeat_count', 1, 1000, 4)]:
            value = float(settings.get(key, default))
            if not math.isfinite(value) or value != int(value) or not lo <= value <= hi:
                raise ValueError('Nieprawidlowe ustawienia impozycji.')
            settings[key] = int(value)
        for key in list(settings):
            if str(settings[key]).lower() in ('true', 'false'):
                settings[key] = truth(settings[key])
            elif key.endswith('_mm'):
                value = float(settings[key])
                if not math.isfinite(value) or not 0 <= value <= 2000:
                    raise ValueError('Nieprawidlowy wymiar impozycji.')
                settings[key] = value
        args = SimpleNamespace(input=src, output=out, **settings)
        if 'imposer_preview_sheet' in step:
            # Same plan and placement as export; only blank positions get a preview fill.
            with fitz.open(src) as document, fitz.open() as preview:
                render_sheets(document, settings_from_args(args), preview, int(step['imposer_preview_sheet']))
                preview.save(out, garbage=4, deflate=True)
            return
        return process_imposer_pdf(args)
    functions = {'bleed': engine.process_bleed_pdf, 'black': engine.process_black_pdf,
        'white': engine.process_white_pdf, 'matrix': engine.process_pdf,
        'shape': engine.process_generated_shape_pdf, 'dieline': engine.process_dieline_pdf,
        'remove': engine.process_remove_color_pdf, 'both': engine.process_combined_pdf,
        'format_dieline': engine.process_format_by_dieline_pdf}
    if mode not in functions:
        raise ValueError('Nieznana operacja.')
    args = arguments(step, src, out)
    args.mode = mode
    if mode == 'remove':
        args.remove_selected = True
    if mode == 'shape':
        args.shape_generator_enabled = True
    if mode == 'white' and args.white_source_type == 'separate_page':
        return engine.process_white_from_separation_pdf(args)
    return functions[mode](args)


if __name__ == '__main__':
    try:
        steps = json.load(sys.stdin)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise ValueError('Kolejka musi zawierac od 1 do 12 operacji.')
        src, final = Path(sys.argv[1]), Path(sys.argv[2])
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError('Nieprawidlowy opis operacji.')
            out = final if index == len(steps) - 1 else final.parent / f'step-{index}.pdf'
            run(step, src, out)
            src = out
    except Exception as error:
        print(json.dumps({'error': str(error)[:500]}, ensure_ascii=True), file=sys.stderr)
        sys.exit(1)
