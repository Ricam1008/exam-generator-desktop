#!/usr/bin/env python3
"""Compare local Vision and a local Granite snapshot on a fixed bilingual corpus.

Run with the packaged backend Python, for example:
  python scripts/benchmark_document_ocr.py --corpus /tmp/exam-ocr --create-fixtures \
      --granite-path /path/to/granite-docling-258M --output /tmp/ocr-results.json
No network requests or model downloads are performed. A missing model is reported,
not replaced with another engine. Each engine runs in its own process for RSS.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from exam_backend import documents

FIXTURES = [
    ('german', ['Gedächtnis und Lernen', 'Das Arbeitsgedächtnis verarbeitet neue Informationen.', 'Wiederholung unterstützt die langfristige Speicherung.', 'Aufmerksamkeit beeinflusst die Aufnahme von Inhalten.']),
    ('english', ['Learning and attention', 'Working memory has limited capacity.', 'Retrieval practice improves long term retention.', 'The study compares younger and older participants.']),
    ('table', ['Tabelle: Reaktionszeiten', 'Gruppe        Mittelwert        Einheit', 'Kontrolle     420               ms', 'Training      360               ms', 'Differenz     60                ms']),
    ('formula', ['Statistik und Formeln', 'Mittelwert = Summe / Anzahl', 'Varianz = 16', 'Standardabweichung = 4', 'Stichprobe n = 25']),
    ('rotated', ['Entwicklungspsychologie', 'Kinder lernen durch Beobachtung.', 'Soziale Erfahrungen beeinflussen die Entwicklung.', 'Die Untersuchung umfasst drei Altersgruppen.']),
    ('mixed', ['Experiment zur Aufmerksamkeit', 'Phase A: Lernmaterial lesen.', 'Phase B: Fragen ohne Unterlagen beantworten.', 'Ergebnis: Wiederholtes Abrufen verbessert die Erinnerung.']),
]


def normalized(text: str) -> str:
    return ' '.join(re.findall(r'\w+', text.casefold(), flags=re.UNICODE))


def metrics(expected: str, actual: str, anchors: list[str]) -> dict:
    gold, found = normalized(expected), normalized(actual)
    required, extracted = collections.Counter(gold.split()), collections.Counter(found.split())
    recall = sum((required & extracted).values()) / max(1, sum(required.values()))
    positions = [found.find(normalized(anchor)) for anchor in anchors]
    order = sum(1 for a,b in zip(positions,positions[1:]) if 0 <= a < b) / max(1,len(positions)-1)
    return {'word_recall': round(recall, 4), 'character_similarity': round(difflib.SequenceMatcher(None,gold,found).ratio(),4), 'reading_order': round(order,4)}


def create_fixtures(directory: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'manifest.json').exists():
        raise ValueError('Fixture manifest already exists; choose a new corpus directory.')
    fonts = ['/System/Library/Fonts/Supplemental/Courier New.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf']
    font = next((ImageFont.truetype(path,28) for path in fonts if Path(path).exists()),ImageFont.load_default(size=28))
    manifest=[]
    for name,lines in FIXTURES:
        image=Image.new('RGB',(1300,650),'white'); draw=ImageDraw.Draw(image)
        for index,line in enumerate(lines): draw.text((45,45+index*90),line,fill='black',font=font)
        if name=='table':
            for y in (115,205,295,385,475): draw.line((35,y,1180,y),fill='#777777',width=1)
        if name=='mixed': draw.rectangle((1050,180,1180,360),outline='black',width=3)
        if name=='rotated': image=image.rotate(90,expand=True)
        image.save(directory/f'{name}.png')
        manifest.append({'file':f'{name}.png','expected':'\n'.join(lines),'anchors':lines,'category':name})
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')


def granite_runtime(model_path: Path):
    import torch
    from transformers import AutoProcessor, AutoModelForVision2Seq
    from docling_core.types.doc import DoclingDocument
    from docling_core.types.doc.document import DocTagsDocument
    processor=AutoProcessor.from_pretrained(str(model_path),local_files_only=True,trust_remote_code=False)
    device='mps' if torch.backends.mps.is_available() else 'cpu'
    model=AutoModelForVision2Seq.from_pretrained(str(model_path),local_files_only=True,trust_remote_code=False,torch_dtype=torch.float32).to(device)
    model.eval()
    def recognize(image):
        messages=[{'role':'user','content':[{'type':'image'},{'type':'text','text':'Convert this page to docling.'}]}]
        prompt=processor.apply_chat_template(messages,add_generation_prompt=True)
        inputs=processor(text=prompt,images=[image],return_tensors='pt').to(device)
        with torch.inference_mode(): output=model.generate(**inputs,max_new_tokens=4096,do_sample=False)
        ids=output[:,inputs.input_ids.shape[1]:]
        if ids.shape[1]>=4096: raise ValueError('Granite output truncated at the token limit.')
        doctags=processor.batch_decode(ids,skip_special_tokens=False)[0].lstrip()
        document=DoclingDocument.load_from_doctags(DocTagsDocument.from_doctags_and_image_pairs([doctags],[image]),document_name='OCR fixture')
        return document.export_to_markdown()
    return recognize


def worker(args) -> None:
    # These libraries honor explicit offline mode, in addition to local_files_only.
    os.environ['HF_HUB_OFFLINE']='1'; os.environ['TRANSFORMERS_OFFLINE']='1'
    start=time.perf_counter()
    recognize=granite_runtime(args.granite_path) if args.worker=='granite' else documents._image_text
    initialization=time.perf_counter()-start
    manifest=json.loads((args.corpus/'manifest.json').read_text(encoding='utf-8'))
    rows=[]
    for fixture in manifest:
        image=documents._open_image(args.corpus/fixture['file'])
        started=time.perf_counter()
        try:
            text=recognize(image)
            rows.append({'file':fixture['file'],'category':fixture['category'],'seconds':round(time.perf_counter()-started,3),'text':text,**metrics(fixture['expected'],text,fixture['anchors'])})
        except Exception as exc:
            rows.append({'file':fixture['file'],'category':fixture['category'],'seconds':round(time.perf_counter()-started,3),'error':str(exc),'word_recall':0,'character_similarity':0,'reading_order':0})
        finally: image.close()
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    model_bytes=sum(p.stat().st_size for p in args.granite_path.rglob('*') if p.is_file()) if args.worker=='granite' else 0
    args.output.write_text(json.dumps({'engine':args.worker,'initialization_seconds':round(initialization,3),'peak_rss_bytes':rss if sys.platform=='darwin' else rss*1024,'local_model_bytes':model_bytes,'downloads_performed':False,'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--create-fixtures',action='store_true')
    parser.add_argument('--granite-path',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',choices=['native','granite'],help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:
        worker(args); return 0
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.create_fixtures: create_fixtures(args.corpus)
    report={'fixture_version':1,'measured_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'platform':sys.platform,'versions':documents.tool_status()['versions'],'engines':[],'enhanced_qualified':False}
    for engine in ['native','granite']:
        if engine=='granite' and not args.granite_path:
            report['engines'].append({'engine':'granite','status':'not_run','reason':'No local Granite snapshot supplied.'}); continue
        result_path=args.output.with_name(args.output.stem+f'-{engine}.json')
        command=[sys.executable,__file__,'--worker',engine,'--corpus',str(args.corpus),'--output',str(result_path)]
        if args.granite_path: command+=['--granite-path',str(args.granite_path.resolve())]
        result=subprocess.run(command,capture_output=True,text=True,timeout=1800)
        if result.returncode:
            report['engines'].append({'engine':engine,'status':'failed','reason':result.stderr[-3000:]})
        else:
            report['engines'].append({'status':'measured',**json.loads(result_path.read_text(encoding='utf-8'))})
    native,enhanced=report['engines']
    if native['status']==enhanced['status']=='measured':
        score=lambda data,key:sum(row[key] for row in data['rows'])/max(1,len(data['rows']))
        no_errors=not any('error' in row for data in [native,enhanced] for row in data['rows'])
        # Require improvement on this exact corpus, no ordering regression, and bounded Mac memory.
        report['enhanced_qualified']=bool(no_errors and score(enhanced,'word_recall')>=score(native,'word_recall') and score(enhanced,'character_similarity')>score(native,'character_similarity')+0.02 and score(enhanced,'reading_order')>=score(native,'reading_order') and enhanced['peak_rss_bytes']<=8*1024**3)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Wrote measured comparison to {args.output}. Enhanced qualified: {report["enhanced_qualified"]}')
    return 0 if all(e['status']=='measured' for e in report['engines']) else 2

if __name__=='__main__': raise SystemExit(main())
