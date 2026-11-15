import sys, types, time, asyncio, tempfile, os
from pathlib import Path
sys.path.insert(0, ''+__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))+'')
# заглушки тяжёлых зависимостей, которых нет в песочнице
st = types.ModuleType('sentence_transformers'); st.SentenceTransformer = object
sys.modules['sentence_transformers'] = st
fa = types.ModuleType('faiss'); sys.modules['faiss'] = fa
try:
    import GCN.memory_graph as mg
except Exception as e:
    print('IMPORT FAILED:', type(e).__name__, e); raise
print('import ok')
# собираем объект без __init__: проверяем именно изменённые методы
obj = mg.CognitiveMemory.__new__(mg.CognitiveMemory)
obj.synapses = {}
obj.facts_by_id = {}
obj.gcn_store = None
obj._dirty = False
obj._dynamic_weights = {'semantic':0.40,'graph':0.30,'freshness':0.15,'confidence':0.05,'evidence':0.10}
obj._sync_synapse_to_gcn = lambda *a, **k: None
def mk(src, tgt, w=0.1, plasticity=0.5, cnt=0):
    s = mg.Synapse(src, tgt, weight=w, plasticity=plasticity, coactivation_count=cnt)
    obj.synapses[(src, tgt)] = s
    return s
# 1) STDP: причинный порядок усиливает, обратный ослабляет (раньше оба давали одинаковую ветку)
s_fwd = mk(1, 2); s_bwd = mk(2, 1)
t0 = 1000.0
obj._hebbian_update(1, 2, t0, t0 + 1.0)   # пре=1 раньше пост=2
obj._hebbian_update(2, 1, t0 + 1.0, t0)   # пре=2 позже пост=1 => dt<0 для 2->1
print('fwd weight', round(s_fwd.weight, 5), 'bwd weight', round(s_bwd.weight, 5))
assert s_fwd.weight > 0.1 and s_bwd.weight < 0.1 + 1e-9 or s_bwd.weight < s_fwd.weight
# 2) метапластичность: пластичность падает с подтверждениями
p_before = s_fwd.plasticity
for i in range(30):
    obj._hebbian_update(1, 2, t0 + i * 100, t0 + i * 100 + 1.0)
print('plasticity', round(p_before, 3), '->', round(s_fwd.plasticity, 3), 'count', s_fwd.coactivation_count)
assert s_fwd.plasticity < p_before
# 3) синаптическое масштабирование: хаб с большой входящей силой ограничивается
obj.synapses = {}
for i in range(1, 11):
    mk(i, 0, w=1.0)
changed = obj._scale_synapses()
inc = sum(s.weight for (a, b), s in obj.synapses.items() if b == 0)
print('hub incoming after scale', round(inc, 4), 'changed', changed)
assert inc <= 3.0 + 1e-9 and changed > 0
# 4) learned rerank: метод создаётся, веса нормированы
with tempfile.TemporaryDirectory() as d:
    obj.base_dir = Path(d)
    rr = obj._get_reranker()
    w = obj._dynamic_weights
    print('dynamic weights', {k: round(v, 3) for k, v in w.items()}, 'sum', round(sum(w.values()), 6))
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert os.path.isdir(d)

# 5) обратная связь обучает веса: сценарий, где семантика надёжна, граф — шум
import datetime
class _Obj:
    def __init__(self, conf): 
        self.confidence=conf; self.evidence=[]; self.created=datetime.datetime.now(datetime.timezone.utc)
class _Store:
    def __init__(self): self.objs={}
    def get(self, gid): return self.objs.get(gid)
    def verify(self,*a,**k): pass
    def compute_confidence(self,gid): return 0.6
    def update(self,*a,**k): pass
store=_Store()
obj.gcn_store=store
obj.user_id='u'
obj._find_fact_by_gcn_id=lambda gid: None
with tempfile.TemporaryDirectory() as d:
    obj.base_dir=Path(d); obj._rerank=None
    before=dict(obj._get_reranker().weights())
    for i in range(40):
        good=f'g{i}'; bad=f'b{i}'
        store.objs[good]=_Obj(0.7); store.objs[bad]=_Obj(0.7)
        obj._record_retrieval_features([
            {'gcn_id':good,'semantic_sim':0.9,'activation':0.05},
            {'gcn_id':bad,'semantic_sim':0.1,'activation':0.9}])
        obj.apply_feedback(good,'confirm')
        obj.apply_feedback(bad,'refute')
    after=obj._dynamic_weights
    print('semantic', round(before['semantic'],3),'->',round(after['semantic'],3),' graph', round(before['graph'],3),'->',round(after['graph'],3))
    assert after['semantic']>before['semantic'] and after['graph']<before['graph']
    assert abs(sum(after.values())-1.0)<1e-6
    # перезапуск: веса подхватываются из файла
    obj._rerank=None; obj._dynamic_weights={'semantic':0.4,'graph':0.3,'freshness':0.15,'confidence':0.05,'evidence':0.1}
    obj._get_reranker()
    print('reloaded semantic', round(obj._dynamic_weights['semantic'],3))
    assert abs(obj._dynamic_weights['semantic']-after['semantic'])<1e-6
print('ALL SMOKE CHECKS PASSED')
