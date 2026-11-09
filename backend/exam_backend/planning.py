"""Selection-aware workload planning and measured, dependency-aware time ranges."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from . import generate_exams as g, generate_final_exams as finals, providers, runtime

VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def selected_sources(sources, payload):
    if 'source_ids' in payload:
        ids = payload['source_ids']
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or not set(ids) <= {s['id'] for s in sources}:
            raise ValueError('Die Materialauswahl hat sich geändert. Bitte erneut einlesen.')
        sources = [s for s in sources if s['id'] in ids]
    return sources[:1] if payload.get('mode', 'example') == 'example' else sources


def content_settings(payload, options):
    settings = dict(options, profile_id=payload.get('profile_id') or 'ollama', model=payload.get('model') or 'gemma4:31b-cloud', profile_snapshot=payload.get('profile_snapshot'), extraction_modes=payload.get('extraction_modes', {}))
    if (effort := deepseek_effort(payload)) is not None:
        settings['reasoning_effort'] = effort
    return settings


def deepseek_effort(payload):
    profile = payload.get('profile_snapshot') or next((p for p in providers.DEFAULT_PROFILES if p['id'] == payload.get('profile_id', 'ollama')), {})
    if profile.get('kind') == 'deepseek':
        return providers.validate_reasoning_effort(payload.get('reasoning_effort', providers.DEFAULT_REASONING_EFFORT))
    return None


def input_signature(payload, sources, options):
    return digest({'version': VERSION, 'sources': [(s['id'], s['fingerprint'], payload.get('course_assignments', {}).get(s['id'], s['course'])) for s in sources], 'settings': content_settings(payload, options), 'output': str(Path(payload.get('output_path') or '~/Documents/Exam Generator Output').expanduser().resolve()), 'location': payload.get('location_override', 'auto'), 'exam_slots': payload.get('max_parallel_exams'), 'request_slots': payload.get('max_active_requests'), 'overwrite': payload.get('overwrite', False)})


def recommendation(execution, benchmark=None):
    cloud = execution.get('location') == 'cloud'
    result = {'exam_slots': 2 if cloud else 1, 'request_slots': 4 if cloud else 1, 'reasons': ['Cloud: zwei Prüfungen, bis zu vier Anfragen. Kontolimits sind nicht automatisch bekannt.' if cloud else 'Ohne vergleichbaren Paralleltest zunächst eine Prüfung und eine KI-Anfrage. Mehr Parallelität kann Speicher und Laufzeit erhöhen.']}
    comparisons = (benchmark or {}).get('concurrency_comparison', [])
    measured = {c.get('active_requests'): c for c in comparisons if c.get('valid') and c.get('duration_seconds', 0) > 0}
    if execution.get('location') == 'local' and 1 in measured and 2 in measured and measured[2]['duration_seconds'] < measured[1]['duration_seconds'] * .85:
        result.update(exam_slots=2, request_slots=2, reasons=['Die synthetische Stichprobe war mit zwei Anfragen mindestens 15 % schneller. Speicher und Qualität bei größeren Prüfungen weiter beobachten.'])
    return result


def request_limits(payload, execution=None, benchmark=None):
    recommended = recommendation(execution or {}, benchmark)
    values = []
    for key, default, maximum in [('max_parallel_exams', recommended['exam_slots'], 2), ('max_active_requests', recommended['request_slots'], 4)]:
        value = payload.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{key}: ganze Zahl zwischen 1 und {maximum} erforderlich.')
        values.append(value)
    if payload.get('location_override', 'auto') not in {'auto', 'local', 'cloud', 'remote', 'unknown'}:
        raise ValueError('Ungültiger Ausführungsort.')
    return tuple(values)


def workspace(payload):
    from . import cli
    root = Path(payload['input_path']).expanduser().resolve()
    output = Path(payload.get('output_path') or cli.DEFAULT_OUTPUT_DIR).expanduser().resolve()
    wid = hashlib.sha256((str(root) + '\0' + str(output)).encode()).hexdigest()[:16]
    return wid, output / f'{cli.slugify(root.name)}--{wid}'


def manifest_at(destination):
    if destination.is_symlink():
        raise ValueError('Der Arbeitsbereich darf kein symbolischer Link sein.')
    try:
        value = json.loads((destination / 'workspace.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def task_groups(extracted, mode):
    if mode != 'finals':
        return [(item[2], [item]) for item in extracted]
    groups = {}
    for item in extracted:
        groups.setdefault(item[2], []).append(item)
    return list(groups.items())


def exam_record(course_dir, items, mode, signature):
    source, _, _, synthetic = items[0]
    key = 'final:' + course_dir.name if mode == 'finals' else source['id']
    fingerprint = hashlib.sha256(''.join(item[0]['fingerprint'] + hashlib.sha256(item[1]['text'].encode()).hexdigest() for item in items).encode()).hexdigest()
    exam_dir = course_dir / 'exams' / ('final-exam' if mode == 'finals' else synthetic.stem)
    return key, fingerprint, exam_dir


def is_reused(entry, fingerprint, signature, exam_dir, overwrite=False):
    return not overwrite and entry.get('fingerprint') == fingerprint and entry.get('settings') == signature and (exam_dir / 'exam.json').is_file()


def source_entries(items):
    return [dict(source_pdf=s['name'], exam_folder=s['id'], text=d['text'], words=g.count_words(d['text']), warning=' '.join(d.get('warnings', [])) or None, pages_total=d.get('page_count') or 0, visuals_detected=0) for s, d, _, _ in items]


def targets(docs, mode, args):
    if mode == 'finals':
        entries = docs
        text = finals.aggregate_source(entries)
        weak = sum(bool(e['warning']) or e['words'] < 800 for e in entries)
        mc, opened, _ = finals.adaptive_targets(args, entries, finals.count_words(text), weak)
        return mc, opened, text, 'finals'
    doc = docs[0]
    text = doc['text']
    coverage = g.resolve_coverage_mode(text, args)
    plan = g.resolve_target_plan(text, g.count_words(text), doc.get('page_count'), max(1, len(g.chunk_text(text))), coverage, args)
    return int(plan['target_multiple_choice']), int(plan['target_open_ended']), text, coverage


def phases(text, mode, mc, opened, args):
    nodes = []
    def add(phase, chars, output, deps):
        key = str(len(nodes))
        nodes.append({'id': key, 'phase': phase, 'prompt_chars': chars, 'output_tokens': output, 'deps': deps})
        return key
    if mode == 'finals':
        deps = []
        for chunk in finals.chunk_text(text, finals.BLUEPRINT_CHARS):
            deps = [add('blueprint', len(chunk) + 1700, 1600, deps)]
        for kind, target, batch, tokens in [('mc', mc, args.mc_batch_size, 220), ('open', opened, args.open_batch_size, 400)]:
            previous = deps
            for index in range(math.ceil(target / batch)):
                count = min(batch, target - index * batch)
                builder = finals.mc_prompt if kind == 'mc' else finals.open_prompt
                excerpt = finals.selected_source_for_batch(finals.chunk_text(text, finals.QUESTION_SOURCE_CHARS), index if kind == 'mc' else index + 4)
                chars = len(builder('', {}, excerpt, count, index + 1, []))
                # Blueprint size and later stems are unknown until generation; runtime checks the actual prompt.
                previous = [add(kind, chars, count * tokens, previous)]
    elif mode == 'full_coverage':
        chunks = g.chunk_text(text)
        for chunk in chunks:
            add('exam', len(chunk) + 8000, math.ceil(mc / len(chunks)) * 220 + math.ceil(opened / len(chunks)) * 400, [])
    else:
        add('exam', len(g.build_generation_prompt('', '', g.chunk_for_prompt(text), mc, opened, None)), mc * 220 + opened * 400, [])
    return nodes


def describe_tasks(extracted, options, signature, manifest, destination, payload):
    from . import cli
    exams = []
    for course_dir, items in task_groups(extracted, options['mode']):
        key, fingerprint, exam_dir = exam_record(course_dir, items, options['mode'], signature)
        args = cli.final_args(destination) if options['mode'] == 'finals' else cli.generator_args(destination, example=options['mode'] == 'example')
        args.target_mode, args.coverage_mode = options['target_mode'], options['coverage_mode']
        if options['target_mode'] == 'manual':
            args.target_mc, args.target_open = options['target_mc'], options['target_open']
        docs = source_entries(items) if options['mode'] == 'finals' else [items[0][1]]
        mc, opened, text, coverage = targets(docs, options['mode'], args)
        nodes = phases(text, coverage, mc, opened, args)
        exams.append({'key': key, 'course': payload.get('course_assignments', {}).get(items[0][0]['id'], items[0][0]['course']), 'sources': [{k: item[0][k] for k in ('id', 'name', 'relative_path')} for item in items], 'available': True, 'reused': is_reused(manifest.get('exams', {}).get(key, {}), fingerprint, signature, exam_dir, payload.get('overwrite')), 'target_mc': mc, 'target_open': opened, 'nominal_calls': len(nodes), 'phases': nodes})
    return exams


def metrics_key(payload, execution, request_slots, exam_slots=None):
    return digest({'version': VERSION, 'provider': payload.get('profile_snapshot'), 'model': payload.get('model'), 'identity': execution.get('model_identity'), 'context': execution.get('context_length'), 'hardware': execution.get('hardware'), 'location': execution.get('location'), 'request_slots': request_slots, 'exam_slots': exam_slots or request_limits(payload, execution)[0], 'language': payload.get('language', 'de'), 'difficulty': payload.get('difficulty', 'balanced'), 'mode': payload.get('mode', 'example'), 'reasoning_effort': deepseek_effort(payload)})


def load_measurements(key):
    try:
        data = json.loads((providers.settings_dir() / 'planning-metrics.json').read_text(encoding='utf-8'))
        return [r for r in data.get('records', []) if isinstance(r, dict) and r.get('key') == key][-300:] if data.get('version') == VERSION else []
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def save_measurements(key, records):
    path = providers.settings_dir() / 'planning-metrics.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get('version') != VERSION:
        data = {'version': VERSION, 'records': []}
    existing = [r for r in data.get('records', []) if isinstance(r, dict)] if isinstance(data.get('records'), list) else []
    # Only numerical telemetry is retained; prompts and file paths never enter history.
    allowed = {'phase', 'seconds', 'prompt_chars', 'output_chars', 'status', 'timing', 'created_at', 'exam_status'}
    data['records'] = (existing + [dict({k: v for k, v in r.items() if k in allowed}, key=key) for r in records])[-1000:]
    runtime.atomic_json(path, data)


def estimate(exams, history, exam_slots, request_slots):
    nodes = [(exam['key'] + ':' + n['id'], exam['key'], n) for exam in exams if exam.get('available') and not exam.get('reused') for n in exam['phases']]
    samples = [r for r in history if r.get('status') == 'response' and r.get('exam_status', 'generated') in {'generated', 'synthetic'} and all(type(r.get(k)) in (int, float) and math.isfinite(r[k]) and r[k] >= 0 for k in ('seconds', 'prompt_chars', 'output_chars')) and r['seconds'] > 0]
    result = {'low_seconds': None, 'high_seconds': None, 'basis': 'Noch nicht kalibriert: keine vergleichbaren Messungen.', 'sample_count': len(samples), 'measured_at': max((r.get('created_at', '') for r in samples), default=None)}
    if not nodes:
        return dict(result, low_seconds=0, high_seconds=0, basis='Keine neue KI-Generierung erforderlich.')
    durations = {}
    for key, _, node in nodes:
        matching = [r for r in samples if r.get('phase') == node['phase']]
        if not matching:
            return result
        durations[key] = statistics.median(r['seconds'] * max(0.5, min(4.0, (node['prompt_chars'] / 3 + node['output_tokens']) / max(1, r.get('prompt_chars', 0) / 3 + r.get('output_chars', 0) / 3))) for r in matching)
    done, running, active_exams = set(), {}, set()
    now = 0.0
    while len(done) < len(nodes):
        for key, exam, node in nodes:
            if key in done or key in running or len(running) >= request_slots:
                continue
            if exam not in active_exams and len(active_exams) >= exam_slots:
                continue
            if not all(exam + ':' + dep in done for dep in node['deps']):
                continue
            active_exams.add(exam)
            running[key] = now + durations[key]
        if not running:
            raise ValueError('Ungültige Abhängigkeiten im Prüfungsplan.')
        now = min(running.values())
        finished = [key for key, end in running.items() if end <= now]
        for key in finished:
            running.pop(key)
            done.add(key)
        for exam in tuple(active_exams):
            if all(key in done for key, owner, _ in nodes if owner == exam):
                active_exams.remove(exam)
    failures = sum(r.get('status') != 'response' for r in history)
    synthetic = any(r.get('exam_status') == 'synthetic' for r in samples)
    sparse = len(samples) < 6 or synthetic
    reserve = (2.0 if sparse else 1.35) + min(0.65, failures / max(1, len(history)))
    basis = 'Vorläufige Hochrechnung aus synthetischer Stichprobe; größere Quellen können deutlich abweichen.' if synthetic else 'Heuristischer Bereich aus vergleichbaren Phasenmessungen; einschließlich Reserve für Wiederholungen. Extraktion separat.'
    return dict(result, low_seconds=math.ceil(now * (.5 if sparse else .75)), high_seconds=math.ceil(now * reserve), basis=basis)


def calibration_measurements(benchmark, request_slots):
    if not benchmark or benchmark.get('timing_applicable') is False or benchmark.get('status') not in {'sample_passed', 'limited'} or any(c.get('ok') is False for c in benchmark.get('checks', [])):
        return []
    return [{'phase': r['phase'].removeprefix('calibration_'), 'seconds': r['duration_seconds'], 'prompt_chars': r['prompt_chars'], 'output_chars': r['response_chars'], 'status': 'response', 'exam_status': 'synthetic', 'created_at': benchmark.get('measured_at', '')} for r in benchmark.get('observations', []) if r.get('concurrency') == 1]


def material_summary(documents):
    totals = {'documents': len(documents), 'words': sum(d.get('words', 0) for d in documents), 'characters': sum(d.get('characters', 0) for d in documents), 'size_bytes': sum(d.get('size_bytes', 0) for d in documents)}
    for name in ('pages', 'slides', 'image_occurrences', 'tables'):
        applicable = [d.get('metadata', {}).get(name) for d in documents if not d.get('failed')]
        # Mixed formats may have no concept of pages/slides; sum measured values and expose unknowns.
        measured = [v for v in applicable if type(v) is int]
        totals[name] = sum(measured) if measured else None
        totals[name + '_unknown_documents'] = sum(v is None for v in applicable)
    duplicates = {}
    for doc in documents:
        duplicates.setdefault(doc.get('fingerprint'), []).append(doc.get('relative_path', doc['name']))
    warnings = ['Identische Dateien an mehreren Orten: ' + ', '.join(paths) for fingerprint, paths in duplicates.items() if fingerprint and len(paths) > 1]
    alternatives = {}
    for doc in documents:
        alternatives.setdefault((doc.get('course'), Path(doc['name']).stem.casefold()), []).append(doc)
    warnings += ['Mögliche alternative Dateiformate desselben Materials prüfen: ' + ', '.join(d['relative_path'] for d in group) for group in alternatives.values() if len({Path(d['name']).suffix.lower() for d in group}) > 1]
    return {'documents': documents, 'totals': totals, 'warnings': warnings}
