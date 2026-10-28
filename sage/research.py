import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.results=[]; self.current=None; self.capture=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a' and 'result__a' in attrs.get('class',''):
            self.current={'title':'','url':attrs.get('href',''),'snippet':''}; self.capture='title'
        elif self.current and tag in ('a','div') and 'result__snippet' in attrs.get('class',''):
            self.capture='snippet'
    def handle_data(self,data):
        if self.current and self.capture: self.current[self.capture]+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.current and self.capture=='title':
            if self.current['title'].strip(): self.results.append(self.current)
            self.current=None; self.capture=None

def search(query, limit=5):
    if not query.strip(): raise ValueError('Search query is empty')
    # Bing's public RSS search endpoint avoids scraping a JavaScript page and is
    # intended for personal, non-commercial feed use. Only source metadata is kept.
    url='https://www.bing.com/search?format=rss&q='+urllib.parse.quote_plus(query)
    req=urllib.request.Request(url,headers={'User-Agent':'SageLocal/0.1 (personal assistant RSS reader)'})
    with urllib.request.urlopen(req,timeout=12) as response: raw=response.read(1_000_000)
    root=ET.fromstring(raw)
    channel=root.find('channel')
    results=[]
    for item in channel.findall('item') if channel is not None else []:
        title=(item.findtext('title') or '').strip(); link=(item.findtext('link') or '').strip()
        snippet=re.sub(r'<[^>]+>',' ',item.findtext('description') or '').strip()
        if title and link.startswith('https://'):
            results.append({'title':title,'url':link,'snippet':snippet})
        if len(results)>=limit: break
    return results

class TextParser(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','nav','footer','header','noscript'): self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','nav','footer','header','noscript') and self.skip: self.skip-=1
    def handle_data(self,data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

def fetch_text(url, limit=12000):
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or not parsed.netloc: return ''
    req=urllib.request.Request(url,headers={'User-Agent':'SageLocal/0.1'})
    with urllib.request.urlopen(req,timeout=10) as response: raw=response.read(500_000)
    parser=TextParser(); parser.feed(raw.decode('utf-8','replace'))
    return re.sub(r'\s+',' ',' '.join(parser.parts))[:limit]

def query_terms(text):
    stop={'about','after','again','also','and','are','can','current','find','from','for','how','into','latest','learn','more','online','please','research','search','should','that','the','their','this','today','what','when','where','which','with','would'}
    terms=set()
    for word in re.findall(r'[a-z0-9]{3,}',text.lower()):
        if word in stop: continue
        if word.endswith('ics') and len(word)>6: word=word[:-3]
        elif word.endswith('s') and len(word)>5: word=word[:-1]
        terms.add(word)
    return terms

def is_relevant(query, source_text):
    terms=query_terms(query)
    if not terms: return True
    source=query_terms(source_text)
    return len(terms & source)>=max(1,(len(terms)+3)//4)

def search_with_config(query):
    from . import config
    from . import llm
    if not config.WEB_SEARCH_ENABLED: return {'query':query,'sources':[],'summary':'Online search is disabled. Set SAGE_WEB_SEARCH_ENABLED=true to enable it.'}
    results=search(query,limit=4)
    sources=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(fetch_text,item['url']):item for item in results}
        for future in as_completed(futures):
            item=futures[future]
            try: text=future.result()
            except Exception: text=item['snippet']
            sources.append({**item,'excerpt':(text or item['snippet'])[:700]})
    sources=[s for s in sources if is_relevant(query,s['title']+' '+s['snippet']+' '+s['excerpt'])]
    sources.sort(key=lambda s: next((i for i,x in enumerate(results) if x['url']==s['url']),99))
    if not sources: return {'query':query,'sources':[],'summary':'No usable search results were returned.'}
    prompt='Synthesize only evidence in the numbered source excerpts below. Cite every sourced claim using [1], [2], etc. If evidence is weak, say so. Return a concise summary and key findings. Do not invent sources.\n\n'+ '\n\n'.join(f"[{i}] {s['title']} — {s['url']}\n{s['excerpt']}" for i,s in enumerate(sources,1))
    summary=llm.chat([{'role':'system','content':'You are a careful research assistant. In at most 180 words, synthesize only the supplied sources, cite each claim with its matching [n] label, distinguish evidence from uncertainty, and mention source disagreement. Do not invent sources.'},{'role':'user','content':f'Research question: {query}\n\n{prompt}'}],timeout=180,max_tokens=180,background=True)
    return {'query':query,'summary':summary,'sources':[{'title':s['title'],'url':s['url']} for s in sources]}
