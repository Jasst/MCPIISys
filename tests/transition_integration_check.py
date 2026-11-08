import sys, types, tempfile, datetime, logging
from pathlib import Path
import numpy as np
sys.path.insert(0, ''+__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))+'')
st = types.ModuleType('sentence_transformers'); st.SentenceTransformer = object; sys.modules['sentence_transformers'] = st
sys.modules['faiss'] = types.ModuleType('faiss')
import GCN.memory_graph as mg
logging.basicConfig(level=logging.WARNING)
rng = np.random.default_rng(0)
K, D = 6, 24
centers = rng.normal(size=(K, D)); centers /= np.linalg.norm(centers, axis=1, keepdims=True)
nxt = np.roll(np.arange(K), 1)            # тема i всегда сменяется темой i+1
topics = [f"тема{i} слово{i} деталь{i}" for i in range(K)]
vec_of = {t: centers[i] + rng.normal(0, 0.03, D) for i, t in enumerate(topics)}
def emb(text):
    for t, v in vec_of.items():
        if t == text: return v
    return centers[0] * 0  # неизвестный текст
obj = mg.CognitiveMemory.__new__(mg.CognitiveMemory)
obj.use_embeddings = True
obj._get_embedding = lambda text, is_query=False: np.asarray(emb(text), dtype=float)
obj.predictive_matrix = {}
obj.concept_examples = {f"c{i}": topics[i] for i in range(K)}
obj.concept_embeddings = {f"c{i}": centers[i] for i in range(K)}
seq = [int(x) for x in rng.integers(0, K, size=120)]
for i, s in enumerate(seq):   # пары: тема s -> тема nxt[s]
    pass
eps = []
for s in seq:
    eps.append(mg.Episode(id=len(eps), user_msg=topics[s], assistant_msg=topics[nxt[s]], timestamp=0.0))
obj.episodic_memory = eps
with tempfile.TemporaryDirectory() as d:
    obj.base_dir = Path(d)
    report = obj._train_transition_model()
    print('report:', report)
    assert report.get('accepted'), report
    assert (Path(d) / 'transition.npz').exists()
    obj._transition_checked = False; obj._transition = None
    tm = obj._get_transition(); assert tm is not None
    import asyncio; pred_out = asyncio.run(obj.predict_next([topics[2]]))
    print('predict_next(тема2) ->', pred_out[:3])
print('TRANSITION INTEGRATION CHECK PASSED')
