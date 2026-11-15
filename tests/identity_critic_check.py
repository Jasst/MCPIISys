import sys, types, ast, asyncio, datetime, logging
sys.path.insert(0,''+__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))+'')
st=types.ModuleType('sentence_transformers'); st.SentenceTransformer=object; sys.modules['sentence_transformers']=st
sys.modules['faiss']=types.ModuleType('faiss')
import numpy as np
import GCN.GCN as gg
logging.basicConfig(level=logging.WARNING)
# подменяем доступ к цепочке идентичности фейковым звеном
class Head:
    id='h1'; author='m'; confidence=1.0; created=datetime.datetime.now(datetime.timezone.utc)
    object={'content':'Я помогаю честно, опираюсь на память и не выдумываю факты.','session_id':None,'open_question':None,'parent_id':None}
gg.get_latest_head=lambda store: Head()
# извлекаем два изменённых метода из routes/ai_assistant.py (сам модуль тяжёлый)
src=open(''+__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))+'/routes/ai_assistant.py',encoding='utf-8').read().replace('\r\n','\n')
i=src.index('    def _identity_head_text(self)')
j=src.index('\n    async def ', src.index('    async def _verify_identity_consistency'))
block='\n'.join(l[4:] if l.startswith('    ') else l for l in src[i:j].split('\n'))
ns={'Optional':__import__('typing').Optional,'logger':logging.getLogger('t'),'np':np}
exec(compile('class C:\n'+'\n'.join('    '+l for l in block.split('\n')),'x','exec'),ns)
C=ns['C']
class Emb:
    # семантический эмбеддер-заглушка: вектор по словам из словаря
    vocab=['память','идентичность','честно','факты','погода','футбол','рецепт','код']
    def embed_text(self,t):
        v=np.array([1.0 if w in t.lower() else 0.0 for w in self.vocab])+0.05
        return v.tolist()
class Shared: pass
class Router: pass
c=C(); c.memory=Emb(); c.router=types.SimpleNamespace(shared_memory=types.SimpleNamespace(store=object()))
on_char=('Я опираюсь на память, говорю честно и проверяю факты, чтобы идентичность оставалась стабильной '
         'и ответы были по существу без выдумок.')
off_char=('Сегодня хороший футбол и погода, рецепт пирога простой: мука, яйца, сахар и немного кода '
          'для таймера на кухне, поэтому давай поговорим о рецептах и погоде.')
r1=asyncio.run(c._verify_identity_consistency(on_char))
r2=asyncio.run(c._verify_identity_consistency(off_char))
print('on-character note:', r1)
print('drift note:', r2)
assert r1 is None and r2 is not None
print('IDENTITY CRITIC CHECK PASSED')
