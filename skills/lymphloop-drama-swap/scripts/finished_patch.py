"""Patch reviewed shots into a finished movie using the existing native renderer.

No model, caption, watermark, brand, selection, or whole-film semantic review is
run here. The old finished movie is the sole source of all unchanged content.
The native renderer re-encodes video atoms and the final AAC soundtrack; this is
content reuse at original speed, never a claim of byte-identical tail copying.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import re

import media_pipeline as media

TYPE = 'lymphloop-finished-master-local-patch'


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def ref(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': media.file_sha256(path)}


def checked(value, label):
    need(isinstance(value, dict) and value.get('path') and value.get('sha256'), label + ': path/SHA required')
    path = Path(value['path']).resolve()
    need(path.is_file() and media.file_sha256(path) == value['sha256'], label + ': changed or missing input')
    return path


def exact(value):
    return Fraction(str(value))


def build_manifest(recipe):
    """Pure metadata validation. Real media validation occurs in native freeze."""
    need(recipe.get('schema_version') == 1 and recipe.get('type') == TYPE, 'Unsupported patch recipe')
    source = checked(recipe.get('source'), 'finished source')
    timeline_path = checked(recipe.get('source_timeline'), 'old finished timeline')
    timeline = read(timeline_path)
    rate = Fraction(timeline['output_fps'])
    count = timeline['output_frames']
    need(isinstance(count, int) and count > 0, 'Invalid old finished frame count')
    owner_path = checked(recipe.get('source_owner'), 'inherited source owner')
    owner = read(owner_path)
    need(owner.get('reviewer') == 'Codex' and owner.get('master_sha256') == recipe['source']['sha256']
         and owner.get('all_flashback_sheets_viewed') is True,
         'The inherited full-film review must bind the exact old finished source')
    replacements = recipe.get('replacements')
    need(isinstance(replacements, list) and 1 <= len(replacements) <= 2, 'Only one or two explicitly reviewed shot replacements')
    pictures, reviews, ids, cursor = [], [owner_path, timeline_path], set(), 0
    ordered = sorted(replacements, key=lambda x: x['old_frames'][0])
    for row in ordered:
        ident = row.get('id')
        need(ident and ident not in ids, 'Unique replacement id required')
        ids.add(ident)
        a, b = row['old_frames']
        need(all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b))
             and cursor <= a < b <= count, ident + ': overlapping/invalid old finished frame window')
        cursor = b
        raw = checked(row.get('raw_media'), ident + ' registered raw')
        clip = checked(row.get('media'), ident + ' captioned replacement')
        ledger_path = checked(row.get('generation_ledger'), ident + ' native ledger')
        ledger = read(ledger_path)
        need(ledger.get('provider') in ('apimart', 'shared_apimart_result'), ident + ': require a native paid or strict shared raw candidate ledger')
        candidate = (ledger.get('candidates') or {}).get(row.get('candidate', 'c1')) or {}
        need(candidate.get('status') == 'downloaded' and candidate.get('output_sha256') == row['raw_media']['sha256']
             and candidate.get('output_path') and media.file_sha256(candidate['output_path']) == row['raw_media']['sha256'],
             ident + ': raw bytes must be a completed registered native candidate')
        review_path = checked(row.get('review'), ident + ' actual local shot/caption review')
        review = read(review_path)
        need(review.get('reviewer') == 'Codex' and review.get('reviewed') is True
             and review.get('raw_media') == row['raw_media'] and review.get('media') == row['media']
             and review.get('own_same_time_audio') is True,
             ident + ': exact raw/captioned byte review and same-time own AV required')
        packet = review.get('caption_audio_packet_sha256') or {}
        need(isinstance(packet.get('before'), str) and re.fullmatch(r'[0-9a-f]{64}', packet['before'])
             and packet.get('before') == packet.get('after'),
             ident + ': native captioning must preserve the reviewed own audio packets')
        need(isinstance(review.get('evidence'), list) and review['evidence'], ident + ': actual viewed evidence required')
        for value in review['evidence']:
            checked(value, ident + ' viewed evidence')
        start, end = map(exact, row['clip_seconds'])
        need(0 <= start < end, ident + ': invalid replacement clip window')
        need(review.get('clip_seconds') == row['clip_seconds'], ident + ': review must bind the exact chosen AV window')
        pictures.append({'id': ident, 'kind': 'Dialogue', 'source_in': a, 'source_out': b,
                         'path': str(clip), 'clip_in': str(start), 'clip_out': str(end),
                         'evidence': 'Exact finished-shot replacement; own same-file/same-time AV. ' + str(review_path)})
        reviews.extend([ledger_path, review_path])
    manifest = {'schema_version': 1, 'source': str(source), 'fps': str(rate), 'output_fps': str(rate),
                'width': timeline['width'], 'height': timeline['height'], 'max_speed': 1,
                'timing': 'ripple', 'picture_replacements': pictures, 'audio_replacements': []}
    return manifest, list(dict.fromkeys(reviews)), timeline


def validate_plan(recipe, plan):
    """Prove complement coverage, order and clocks without any image inference."""
    need(plan['source'] == str(Path(recipe['source']['path']).resolve()), 'Source is not the old finished movie')
    need(plan.get('timing') == 'ripple' and plan['max_speed'] == 1, 'Original-speed ripple is mandatory')
    need(plan.get('source_has_audio') is True, 'Old finished source must have its reviewed soundtrack')
    requested = {x['id']: x for x in recipe['replacements']}
    need(len(plan['segments']) <= 2 * len(requested) + 1, 'Unexpected extra edit atoms')
    old_cursor, out_cursor, seen = 0, 0, set()
    for atom in plan['segments']:
        need(atom['source_in'] == old_cursor and atom['out_in'] == out_cursor, 'Edit reordered or skipped source coverage')
        need(atom['picture_speed'] == atom['source_audio_speed'] == 1, 'Speed change forbidden')
        ident = atom.get('picture_id')
        if ident is None:
            need(atom['kind'] == 'Original' and atom['path'] == plan['source'] and not atom.get('audio_replacement_id'),
                 'Unchanged atom must use the old finished picture and paired audio only')
            need(atom['frames'] == atom['source_out'] - atom['source_in'], 'Unchanged content length changed')
        else:
            need(ident in requested and ident not in seen, 'Unexpected or repeated new shot')
            row = requested[ident]
            need([atom['source_in'], atom['source_out']] == row['old_frames'] and atom['path'] == str(Path(row['media']['path']).resolve()),
                 'Actual replacement differs from reviewed finished window/bytes')
            audio = next(x for x in plan['audio_replacements'] if x['id'] == atom['audio_replacement_id'])
            need(audio['path'] == atom['path'] and audio['clip_in'] == atom['clip_in']
                 and audio['clip_out'] == atom['clip_out'] and audio['speed'] == 1,
                 'Replacement picture and soundtrack must be the same file and same time')
            seen.add(ident)
        old_cursor, out_cursor = atom['source_out'], atom['out_out']
    need(seen == set(requested) and old_cursor == plan['source_frames'] and out_cursor == plan['output_frames'],
         'Patch is not complete exact source coverage')
    expected = plan['source_frames'] - sum(b-a for a, b in (x['old_frames'] for x in requested.values()))
    expected += sum(math.ceil((exact(x['clip_seconds'][1])-exact(x['clip_seconds'][0])) * Fraction(plan['output_fps'])) for x in requested.values())
    need(expected == plan['output_frames'], 'Output frame clock differs from exact selected ripple windows')


def contract(recipe, frozen_path):
    build_manifest(recipe)  # Recheck raw, source timeline and actual viewed evidence as well as frozen inputs.
    frozen = media.verify_frozen_plan(frozen_path)
    plan = frozen['plan']
    validate_plan(recipe, plan)
    rate = Fraction(plan['output_fps'])
    windows, inherited, joins = [], [], set()
    for atom in plan['segments']:
        row = {'old_frames': [atom['source_in'], atom['source_out']], 'new_frames': [atom['out_in'], atom['out_out']],
               'old_seconds': [float(Fraction(atom[k], 1)/rate) for k in ('source_in', 'source_out')],
               'new_seconds': [float(Fraction(atom[k], 1)/rate) for k in ('out_in', 'out_out')],
               'path': atom['path'], 'picture_speed': 1, 'audio_speed': 1}
        if atom.get('picture_id'):
            row.update(id=atom['picture_id'], clip_seconds=[atom['clip_in'], atom['clip_out']], own_same_time_av=True)
            original_row = next(x for x in recipe['replacements'] if x['id'] == atom['picture_id'])
            if original_row.get('segment'):
                row['segment'] = original_row['segment']
            windows.append(row)
        else:
            row['shift_frames'] = atom['out_in'] - atom['source_in']
            inherited.append(row)
        joins.update((atom['out_in'], atom['out_out']))
    return {'schema_version': 1, 'type': TYPE + '-receipt', 'status': 'prepared_not_rendered',
            'adapter_implementation': ref(__file__),
            'source': recipe['source'], 'source_owner': recipe['source_owner'], 'source_timeline': recipe['source_timeline'],
            'frozen_plan': ref(frozen_path), 'old_frames': plan['source_frames'], 'output_frames': plan['output_frames'],
            'fps': plan['output_fps'], 'output_seconds': plan['output_seconds'], 'output_audio_samples': plan['output_samples'],
            'audio_sample_rate': plan['sample_rate'], 'boundary_convention': '[in,out)', 'replacements': windows,
            'unchanged_intervals': inherited, 'join_frames': sorted(joins - {0, plan['output_frames']}),
            'standard_picture_and_audio_speed': 1,
            'export': {'renderer': 'media_pipeline.freeze/render', 'video': 'libx264 fast CRF18; per-atom encode then concat',
                       'audio': 'native 48000Hz PCM clock then AAC 192k', 'reencodes_old_finished_material': True,
                       'tail_bitstream_identity_claimed': False, 'no_caption_or_brand_redraw': True},
            'review_scope': {'new_full_film_semantic_review': False, 'local_replacement_and_join_review_required': True,
                             'unchanged_content_review': 'Inherited exact prior finished-master review through source SHA and original-speed interval mapping.',
                             'technical_check': 'Native render technical frame/CFR/full-decode/audio-clock checks; not a new semantic review.'},
            'timeline_coordinate_system': 'old finished master -> current local patch; not raw original source',
            'source_to_current_mapping': 'Compose source_timeline with this exact old/new interval map. Inside regenerated scenes UI interpolation is navigation only; exact patch windows are listed separately.'}


def prepare(recipe_path, directory):
    recipe_path, directory = Path(recipe_path).resolve(), Path(directory).resolve()
    recipe = read(recipe_path)
    manifest, reviews, old_timeline = build_manifest(recipe)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path, frozen_path, receipt_path = (directory / n for n in ('manifest.json', 'frozen.json', 'receipt.json'))
    need(not any(x.exists() for x in (manifest_path, frozen_path, receipt_path)), 'Prepared directory already has a patch; preserve it')
    media.save(manifest_path, manifest)
    frozen = media.freeze(manifest_path, frozen_path, [recipe_path, *reviews])
    need(frozen['plan']['source_frames'] == old_timeline['output_frames'] and frozen['plan']['output_fps'] == str(Fraction(old_timeline['output_fps'])),
         'Actual source media frame clock differs from archived finished timeline')
    receipt = contract(recipe, frozen_path)
    receipt['recipe'] = ref(recipe_path)
    media.save(receipt_path, receipt)
    return receipt


def render(directory, output, work_dir=None):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    need(not output.exists(), 'Write a distinct new local-patch movie; never overwrite the old delivery')
    prepared = read(directory / 'receipt.json')
    need(prepared.get('status') == 'prepared_not_rendered', 'Patch is not awaiting its first render')
    recipe_path = checked(prepared['recipe'], 'prepared recipe')
    checked(prepared['frozen_plan'], 'prepared frozen plan')
    recipe = read(recipe_path)
    actual = contract(recipe, directory / 'frozen.json')
    need(all(actual[k] == prepared[k] for k in actual), 'Prepared patch contract changed')
    result = media.render(directory / 'manifest.json', output, work_dir, directory / 'frozen.json')
    receipt = {**prepared, 'status': 'rendered_awaiting_local_owner_review', 'output': ref(output),
               'timeline': ref(str(output) + '.timeline.json'), 'verification': ref(str(output) + '.verification.json'),
               'render_result': result}
    media.save(directory / 'receipt.rendered.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--recipe', required=True); p.add_argument('--directory', required=True)
    p = sub.add_parser('render'); p.add_argument('--directory', required=True); p.add_argument('--output', required=True); p.add_argument('--work-dir')
    args = parser.parse_args()
    answer = prepare(args.recipe, args.directory) if args.command == 'prepare' else render(args.directory, args.output, args.work_dir)
    print(json.dumps(answer, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
