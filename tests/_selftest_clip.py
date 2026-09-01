"""
CLIP API-compatibility regression test.

The earlier verification only checked that get_text_features / get_image_features
EXISTED and accepted the right kwargs. It never checked what they RETURN - and in
transformers v5 they return a BaseModelOutputWithPooling rather than a tensor,
which crashed on Brian's machine with:

    AttributeError: 'BaseModelOutputWithPooling' object has no attribute 'norm'

This builds a real (randomly initialised) CLIPModel from config - no download - and
runs the genuine transformers code path through clip_features().
"""
import sys
from pathlib import Path
import torch
import transformers
from transformers import CLIPModel, CLIPConfig

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

print(f"transformers {transformers.__version__}, torch {torch.__version__}\n")

# A real CLIPModel, small enough to build instantly, with the SAME projection
# width as ViT-L/14 so the dimension check is exercised for real.
cfg = CLIPConfig(
    text_config=dict(hidden_size=32, intermediate_size=37, num_hidden_layers=2,
                     num_attention_heads=2, vocab_size=99, max_position_embeddings=64),
    vision_config=dict(hidden_size=32, intermediate_size=37, num_hidden_layers=2,
                       num_attention_heads=2, image_size=32, patch_size=4),
    projection_dim=ps.CLIP_EMBED_DIM,
)
model = CLIPModel(cfg).eval()
ids = torch.randint(0, 99, (5, 7))
mask = torch.ones_like(ids)
px = torch.randn(1, 3, 32, 32)

with torch.no_grad():
    raw_text = model.get_text_features(input_ids=ids, attention_mask=mask)
    raw_img = model.get_image_features(pixel_values=px)

print(f"get_text_features returns : {type(raw_text).__name__}")
print(f"get_image_features returns: {type(raw_img).__name__}\n")

# --- the real code path ------------------------------------------------------
t = ps.clip_features(raw_text, ps.CLIP_EMBED_DIM)
i = ps.clip_features(raw_img, ps.CLIP_EMBED_DIM)
check("text features extracted as a tensor", torch.is_tensor(t), str(tuple(t.shape)))
check("image features extracted as a tensor", torch.is_tensor(i), str(tuple(i.shape)))
check("text width is the projection dim", t.shape[-1] == ps.CLIP_EMBED_DIM)
check("image width is the projection dim", i.shape[-1] == ps.CLIP_EMBED_DIM)
check("one row per prompt", t.shape[0] == 5, f"got {t.shape[0]}")

# The operations that actually crashed for Brian
tn = t / t.norm(dim=-1, keepdim=True)
inn = i / i.norm(dim=-1, keepdim=True)
check("norm() works on extracted text features", torch.isfinite(tn).all())
check("normalised rows are unit length",
      torch.allclose(tn.norm(dim=-1), torch.ones(5), atol=1e-5))
sims = (inn @ tn.T).squeeze(0)
check("image-vs-text similarity matmul works", sims.shape == (5,), str(tuple(sims.shape)))
probs = (sims * 100.0).softmax(dim=-1)
check("softmax over prompts sums to 1", abs(float(probs.sum()) - 1.0) < 1e-5)

# The LAION head must accept the extracted image embedding
import torch.nn as nn
head = nn.Sequential(nn.Linear(ps.CLIP_EMBED_DIM, 1024), nn.Dropout(0.2),
                     nn.Linear(1024, 128), nn.Dropout(0.2), nn.Linear(128, 64),
                     nn.Dropout(0.1), nn.Linear(64, 16), nn.Linear(16, 1)).eval()
with torch.no_grad():
    score = head(inn.float())
check("LAION head accepts the embedding", score.shape == (1, 1), str(tuple(score.shape)))

# --- backward compatibility with transformers v4 (plain tensor) --------------
print()
plain = torch.randn(3, ps.CLIP_EMBED_DIM)
check("v4-style plain tensor passes through",
      ps.clip_features(plain, ps.CLIP_EMBED_DIM) is plain)

class FakeV4Embeds:      # some versions expose .text_embeds
    text_embeds = torch.randn(2, ps.CLIP_EMBED_DIM)
check("`.text_embeds` style handled",
      ps.clip_features(FakeV4Embeds(), ps.CLIP_EMBED_DIM).shape == (2, ps.CLIP_EMBED_DIM))

# --- it must FAIL LOUDLY rather than score garbage ---------------------------
print()
for label, bad, dim in [
    ("wrong embedding width rejected", torch.randn(2, 512), ps.CLIP_EMBED_DIM),
    ("3-D tensor rejected", torch.randn(2, 7, 768), None),
]:
    try:
        ps.clip_features(bad, dim)
        check(label, False, "no exception raised")
    except ValueError as e:
        check(label, True, str(e)[:70] + "...")

try:
    ps.clip_features(object(), None)
    check("unrecognised output type rejected", False, "no exception raised")
except TypeError as e:
    check("unrecognised output type rejected", True, str(e)[:70] + "...")

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
