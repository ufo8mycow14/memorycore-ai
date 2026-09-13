"""Bounded reference graph packets; relations are evidence, never inferred truth."""
from collections import deque
import json
import time
from . import memorycore_ai as bm
from .knowledge_layer import canonical, digest, identifier

def _items(k, kind, mid, limit, intent='related'):
    condition="owner=?3" if kind=='source' else {
        'dependencies':"owner=?3 AND json_extract(payload,'$.relation')='depends_on'",
        'impact':"target=?3 AND json_extract(payload,'$.relation') IN ('depends_on','applies_to')",
        'evidence':"owner=?3 AND json_extract(payload,'$.relation')='supported_by'",
        'conflicts':"(owner=?3 OR target=?3) AND json_extract(payload,'$.relation')='contradicts'",
        'related':'(owner=?3 OR target=?3)'}[intent]
    key=bytes.fromhex(mid)
    rows=k.conn.execute(f'SELECT id,scope,kind,owner,target,CASE WHEN length(CAST(payload AS BLOB))<=16384 THEN payload END AS payload,checksum FROM knowledge_item WHERE scope=?1 AND kind=?2 AND {condition} ORDER BY id LIMIT ?4',(k.scope,kind,key,limit)).fetchall()
    result=[]
    for row in rows:
        if row['payload'] is None:
            if kind=='source': raise ValueError('graph source bound')
            result.append({'id':row['id'],'oversized':True})
            continue
        value={'id':row['id'],'scope':row['scope'],'kind':row['kind'],
               'owner':row['owner'].hex() if row['owner'] else None,
               'target':row['target'].hex() if row['target'] else None,'payload':json.loads(row['payload'])}
        if digest(value)!=row['checksum']: raise ValueError('graph relation integrity failure')
        k._validate(value)
        result.append(value)
    return result

def _node(k, mid, cache):
    row=k._memory(mid)
    sources=_items(k,'source',mid,5)
    if not 0<len(sources)<=4: raise ValueError('graph source bound')
    if k.conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_chat_link'").fetchone():
        permitted=[]
        for source in sources:
            state=k.conn.execute('SELECT c.state FROM native_chat_link l JOIN native_chat c USING(scope,chat_id) WHERE l.scope=? AND l.memory_id=? AND l.path=?',
                (k.scope,bytes.fromhex(mid),source['payload']['path'])).fetchone()
            if state is None or state[0]=='active': permitted.append(source)
        sources=permitted
    fresh=k._freshness(mid,sources,cache)
    if fresh['state']!='fresh': raise ValueError('graph source not fresh')
    payload=bm.verify_memory_row(row)
    return {'id':mid,'subject':payload['subject'],'summary':payload['summary'],
        **{key:row[key] for key in ['confidence','confidence_reason','observed_at','valid_from','valid_to','expires_at']},
        'type':bm.TYPE_NAMES[row['memory_type']],'sources':fresh['sources']}

def _follows(intent, relation, outgoing):
    return (intent=='related' or
        (intent=='dependencies' and outgoing and relation=='depends_on') or
        (intent=='impact' and not outgoing and relation in {'depends_on','applies_to'}) or
        (intent=='evidence' and outgoing and relation=='supported_by') or
        (intent=='conflicts' and relation=='contradicts'))

def recall(k, root, *, intent='related', depth=2, budget=1100, count):
    identifier(root)
    if intent not in {'related','dependencies','impact','evidence','conflicts'} or type(depth) is not int or not 1<=depth<=3 or not 256<=budget<=1240:
        raise ValueError('invalid graph bound')
    k._memory(root)
    started=time.monotonic()
    packet={'format':'memory-graph/1','scope':k.scope,'intent':intent,'depth':depth,
        'data_only':True,'inferred_truth':False,'confidence_scale':255,
        'nodes':[],'edges':[],'truncated':False,'omitted':False}
    cache={}
    try: packet['nodes']=[_node(k,root,cache)]
    except (ValueError,OSError):
        packet['omitted']=True
        return packet
    if count(canonical(packet))+8>budget:
        packet.update(nodes=[],truncated=True)
        return packet
    admitted={root:0}
    inspected={root}
    eligible={}
    seen_edges=set()
    seen_links=set()
    frontier=deque([(root,0)])
    scanned=0
    while frontier:
        current,level=frontier.popleft()
        if level>=depth: continue
        if time.monotonic()-started>=.075:
            packet['truncated']=True
            break
        remaining=64-scanned
        edges=_items(k,'relation',current,remaining+1,intent)
        if len(edges)>remaining: packet['truncated']=True
        for edge in edges[:remaining]:
            scanned+=1
            if time.monotonic()-started>=.075:
                packet['truncated']=True
                return packet
            if edge['id'] in seen_edges: continue
            seen_edges.add(edge['id'])
            if edge.get('oversized'):
                packet['truncated']=True
                continue
            owner,target=edge['owner'],edge['target']
            relation=edge['payload']['relation']
            if not _follows(intent,relation,owner==current): continue
            following=target if owner==current else owner
            link=(owner,target,canonical(edge['payload']))
            if link in seen_links: continue
            seen_links.add(link)
            candidate={**packet,'nodes':list(packet['nodes']),'edges':list(packet['edges'])}
            if following in admitted:
                index=admitted[following]
            else:
                if len(admitted)>=16:
                    packet['truncated']=True
                    continue
                if following not in inspected:
                    if len(inspected)>=32:
                        packet['truncated']=True
                        continue
                    inspected.add(following)
                    try:
                        value=_node(k,following,cache)
                        if count(canonical(value))<=budget: eligible[following]=value
                        else: packet['truncated']=True
                    except (ValueError,OSError): packet['omitted']=True
                if following not in eligible:
                    continue
                candidate['nodes'].append(eligible[following])
                index=len(admitted)
            origin=admitted[current]
            source,destination=(origin,index) if owner==current else (index,origin)
            candidate['edges'].append({'from':source,'to':destination,'relation':relation,'evidence':edge['payload']['evidence']})
            if count(canonical(candidate))+8>budget:
                packet['truncated']=True
                continue
            packet=candidate
            if following not in admitted:
                admitted[following]=index
                eligible.pop(following,None)
                frontier.append((following,level+1))
        if scanned>=64:
            packet['truncated']=True
            break
    if count(canonical(packet))>budget: raise ValueError('graph packet budget')
    return packet
