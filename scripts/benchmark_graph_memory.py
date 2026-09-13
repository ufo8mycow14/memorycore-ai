"""Synthetic native graph latency and equivalent-content packet token comparison."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from scripts.knowledge_layer import Knowledge, SourceRoot, canonical
from scripts.memory_packets import token_counter
from scripts.test_rust_broker import Harness, BINARY, packet
from scripts.benchmark_memory_host import summary

def deduplicated_text(body, full_ids=False):
    rows=[{'metadata':{key:value for key,value in body.items() if key not in {'nodes','edges'}}}]
    rows.extend({'node':node} for node in body['nodes'])
    for edge in body['edges']:
        value=({**edge,'from':body['nodes'][edge['from']]['id'],
                'to':body['nodes'][edge['to']]['id']} if full_ids else edge)
        rows.append({'edge':value})
    return '\n'.join(canonical(row) for row in rows)


def run(output, batches=30, mixed_writes=False, distractors_per_scope=0):
    if output.exists(): raise ValueError('Preserve earlier benchmark')
    if not 0<=distractors_per_scope<=10000 or batches<1:
        raise ValueError('Invalid synthetic benchmark size')
    benchmark_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    h=Harness(0,timestamp_responses=True)
    roots={}
    expected={}
    count=token_counter()
    try:
        for n,s in enumerate(h.config['sessions'][:10]):
            s['allow_admin']=True
            k=Knowledge(h.conn,scope=s['scope'],sources=SourceRoot(s['source_root']),synthetic=True)
            ids=[]
            for index,text in enumerate([
                'The deployment depends on the reviewed launch procedure.',
                'Run the calibration procedure only after approval; never bypass review.',
                'The signed synthetic checklist requires prior approval.',
                'An old draft suggests skipping approval; that instruction is disputed.']):
                name=f'node-{index}.md'
                (Path(s['source_root'])/name).write_text('Fact: '+text+'\n',encoding='utf-8')
                p=k.propose(name)['proposals'][0]
                ids.append(k.accept(p['id'],p['review_digest'])['memory_id'])
            k.relate(ids[0],ids[1],'depends_on','Deployment requires the launch procedure.',reviewed=True)
            k.relate(ids[1],ids[2],'supported_by','The checklist supports the procedure.',reviewed=True)
            k.relate(ids[0],ids[3],'contradicts','The draft conflicts with the approved procedure.',reviewed=True)
            roots[s['id']]=ids[0]
            expected[s['id']]=set(ids)
        if mixed_writes:
            h.config['sessions'].append(dict(h.config['sessions'][0],id='graph-writer'))
        h.config.update(backend='native')
        h.file.write_text(json.dumps(h.config),encoding='utf-8')
        h.start()
        # The reference proposal selector caps scopes at 100 records. Seed growth
        # through the actual native write API, not by bypassing that safety cap.
        for index in range(distractors_per_scope):
            for s in h.config['sessions'][:10]:
                path=f'distractor-{index}.md'
                raw=f'Fact: Synthetic unrelated record {index} retains its reviewed approval requirement.\n'.encode()
                (Path(s['source_root'])/path).write_bytes(raw)
                h.send(s['id'],'admin',{'action':'remember-bound','arguments':{'type':'semantic',
                    'subject':f'Synthetic unrelated {index}','summary':raw.decode().strip(),
                    'source':path,'source_hash':hashlib.sha256(raw).hexdigest()}})
            for _ in range(10):
                if not h.receive().get('result',{}).get('memory_id'):
                    raise ValueError('Native growth fixture write failed')
            if (index+1)%100==0:
                print(json.dumps({'seeded_distractors':10*(index+1)}),flush=True)
        latencies=[]
        compact=[]
        expanded=[]
        deduplicated=[]
        indexed=[]
        packets=[]
        hits=0
        write_latencies=[]
        writes=0
        errors=[]
        for batch in range(batches):
            pending={}
            for session,root in roots.items():
                pending[session]=time.perf_counter()
                h.send(session,'call',{'name':'memory','arguments':{'graph':root,'depth':'2'}})
            if mixed_writes:
                path=f'background-{batch}.md'
                raw=f'Fact: Synthetic background record {batch} uses a reviewed local process.\n'.encode()
                (Path(h.config['sessions'][0]['source_root'])/path).write_bytes(raw)
                pending['graph-writer']=time.perf_counter()
                h.send('graph-writer','admin',{'action':'remember-bound','arguments':{'type':'semantic',
                    'subject':f'Synthetic background {batch}','summary':raw.decode().strip(),
                    'source':path,'source_hash':hashlib.sha256(raw).hexdigest()}})
            for _ in range(len(pending)):
                response=h.receive()
                elapsed=(response['_benchmark_received_at']-pending[response['session']])*1000
                if response['session']=='graph-writer':
                    write_latencies.append(elapsed)
                    if response.get('result',{}).get('memory_id'): writes+=1
                    else: errors.append({'batch':batch,'kind':'write_failed'})
                    continue
                latencies.append(elapsed)
                try:
                    body=packet(response)
                    found={node['id'] for node in body['nodes']}
                    if found==expected[response['session']] and not body['truncated']:
                        hits+=1
                    else: errors.append({'batch':batch,'kind':'missing_extra_or_truncated',
                        'session':response['session'],'node_count':len(found),
                        'truncated':body.get('truncated'),'omitted':body.get('omitted')})
                    packets.append(body)
                    assert all(edge['from']<len(body['nodes']) and edge['to']<len(body['nodes']) for edge in body['edges'])
                except (KeyError,AssertionError,TypeError) as error:
                    errors.append({'batch':batch,'kind':type(error).__name__})
        # Tokenisation must not compete with receiving the timed responses.
        for body in packets:
            redundant={**body,'edges':[{**edge,'from':body['nodes'][edge['from']], 'to':body['nodes'][edge['to']]} for edge in body['edges']]}
            compact.append(count(canonical(body)))
            expanded.append(count(canonical(redundant)))
            deduplicated.append(count(deduplicated_text(body,full_ids=True)))
            indexed.append(count(deduplicated_text(body)))
        integrity=[]
        for session in roots:
            h.send(session,'admin',{'action':'verify','arguments':{}})
        for _ in roots:
            response=h.receive()
            integrity.append(response.get('result',{}).get('verified') is True)
        receipt={'synthetic_only':True,'projects':10,'simultaneous_requests_per_batch':10+int(mixed_writes),
            'requests':batches*10,'exact_graph_hits':hits,'errors':errors,'latency_ms':summary(latencies),
            'mixed_source_bound_writes':mixed_writes,'committed_writes':writes,'write_latency_ms':summary(write_latencies),
            'all_project_integrity':all(integrity),'integrity_scope_count':len(integrity),
            'packet_tokens':summary(compact),'equivalent_repeated_endpoint_tokens':summary(expanded),
            'deduplicated_full_id_text_tokens':summary(deduplicated),
            'deduplicated_indexed_text_tokens':summary(indexed),
            'reduction_vs_deduplicated_full_id_text':1-sum(compact)/sum(deduplicated) if deduplicated else None,
            'reduction_vs_deduplicated_indexed_text':1-sum(compact)/sum(indexed) if indexed else None,
            'distractors_per_scope':distractors_per_scope,
            'initial_total_records':10*(4+distractors_per_scope),
            'representation_token_reduction':1-sum(compact)/sum(expanded) if expanded else None,
            'tokenizer':'o200k_base','binary_sha256':hashlib.sha256(BINARY.read_bytes()).hexdigest(),
            'benchmark_sha256':benchmark_hash,
            'latency_boundary':'Before request serialisation to reader-thread JSON decode; excludes subsequent token counting.',
            'limitations':['Synthetic four-node traversals; distractors test database growth, not large connected-graph capacity.',
                'Token comparison preserves graph content but is a representation baseline, not Codex or a measured model task.',
                'Background source-file creation is excluded from write request latency; one write accompanies each ten-read batch.',
                'Seed retrieval and catalogue overhead are excluded; no billed-token or whole-task saving is claimed.'],
            'production_approved':False}
        with output.open('x',encoding='utf-8') as f: json.dump(receipt,f,indent=2)
        print(json.dumps(receipt,indent=2))
        if errors or hits!=batches*10 or not all(integrity) or (mixed_writes and writes!=batches): raise SystemExit('Graph benchmark failed')
    finally:
        h.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mixed-writes',action='store_true')
    parser.add_argument('--distractors-per-scope',type=int,default=0)
    parser.add_argument('--batches',type=int,default=30)
    args=parser.parse_args()
    run(args.output,batches=args.batches,mixed_writes=args.mixed_writes,distractors_per_scope=args.distractors_per_scope)
