import asyncio
from functools import partial
from pathlib import Path
from typing import List, Optional, Union
import json, argparse, random, time
import tiktoken
from tiktoken.load import load_tiktoken_bpe
from exo.inference.tinygrad.models.llama import Transformer, convert_from_huggingface, fix_bf16
from tinygrad.nn.state import safe_load, torch_load, load_state_dict, get_parameters
from tinygrad import Tensor, dtypes, nn, Context, Device, GlobalCounters
from tinygrad.helpers import DEBUG, tqdm, _cache_dir, fetch
from exo.inference.shard import Shard
from exo.inference.inference_engine import InferenceEngine
import numpy as np
import os

MODEL_PARAMS = {
  "8B": {
    "args": {"dim": 4096, "n_heads": 32, "n_kv_heads": 8, "n_layers": 32, "norm_eps": 1e-5, "rope_theta": 500000, "vocab_size": 128256, "hidden_dim": 14336},
    "files": 1
  },
  "70B": {
    "args": {"dim": 8192, "n_heads": 64, "n_kv_heads": 8, "n_layers": 80, "norm_eps": 1e-5, "rope_theta": 500000, "vocab_size": 128256,  "hidden_dim": 28672},
    "files": 8
  }
}

class Tokenizer:
  pat_str = r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
  def __init__(self, model_path: str):
    mergeable_ranks = load_tiktoken_bpe(model_path)
    self.num_base_tokens = len(mergeable_ranks)
    special_tokens = [
      "<|begin_of_text|>",
      "<|end_of_text|>",
      "<|reserved_special_token_0|>",
      "<|reserved_special_token_1|>",
      "<|reserved_special_token_2|>",
      "<|reserved_special_token_3|>",
      "<|start_header_id|>",
      "<|end_header_id|>",
      "<|reserved_special_token_4|>",
      "<|eot_id|>",
    ] + [
      f"<|reserved_special_token_{i}|>"
      for i in range(5, 256 - 5)
    ]
    self.special_tokens = {token: len(mergeable_ranks) + i for i, token in enumerate(special_tokens)}

    self.model = tiktoken.Encoding(name=model_path, pat_str=self.pat_str, mergeable_ranks=mergeable_ranks, special_tokens=self.special_tokens)

  @property
  def bos_id(self): return self.special_tokens["<|begin_of_text|>"]
  @property
  def stop_tokens(self): return {self.special_tokens["<|end_of_text|>"], self.special_tokens["<|eot_id|>"]}

  def decode(self, toks):
     return self.model.decode([t for t in toks if t < self.num_base_tokens])
  def encode(self, text, allow_special=False):
    return self.model.encode(text, allowed_special="all" if allow_special else set(), disallowed_special=set())

# **** helper functions ****
async def fetch_async(url: str, name: Optional[Union[Path, str]] = None, subdir: Optional[str] = None,
                      allow_caching=not os.getenv("DISABLE_HTTP_CACHE")) -> Path:
    func = partial(fetch, url, name, subdir, allow_caching)
    return await asyncio.get_event_loop().run_in_executor(None, func)

def concat_weights(models, device=None):
  def convert(name) -> Tensor:
    disk_tensors: List[Tensor] = [model[name] for model in models]
    if len(disk_tensors) == 1 or len(disk_tensors[0].shape) == 1:
      return disk_tensors[0].to(device=device)
    axis = 1 if name.endswith(".attention.wo.weight") or name.endswith(".feed_forward.w2.weight") else 0
    lazy_tensors = [data.to(device=device) for data in disk_tensors]
    return lazy_tensors[0].cat(*lazy_tensors[1:], dim=axis)
  return {name: convert(name) for name in {name: None for model in models for name in model}}

def load(fn:str):
  if fn.endswith('.index.json'):
    with open(fn) as fp: weight_map = json.load(fp)['weight_map']
    parts = {n: load(str(Path(fn).parent / Path(n).name)) for n in set(weight_map.values())}
    return {k: parts[n][k] for k, n in weight_map.items()}
  elif fn.endswith(".safetensors"):
    return safe_load(fn)
  else:
    return torch_load(fn)

def build_transformer(model_path: Path, shard: Shard, model_size="8B", quantize=None, device=None):
  # build model
  linear = nn.Linear
  with Context(THREEFRY=0):
    model = Transformer(**MODEL_PARAMS[model_size]["args"], shard=shard, linear=linear, max_context=8192, jit=False)

  # load weights
  if model_path.is_dir():
    if (model_path / "model.safetensors.index.json").exists(): weights = load(str(model_path / "model.safetensors.index.json"))
    elif (model_path / "model.safetensors").exists(): weights = load(str(model_path / "model.safetensors"))
    else: weights = concat_weights([load(str(model_path / f"consolidated.{i:02d}.pth")) for i in range(MODEL_PARAMS[model_size]["files"])], device[0] if isinstance(device, tuple) else device)
  else:
    weights = load(str(model_path))
  if "model.embed_tokens.weight" in weights:
    weights = convert_from_huggingface(weights, model, MODEL_PARAMS[model_size]["args"]["n_heads"], MODEL_PARAMS[model_size]["args"]["n_kv_heads"], shard=shard)
  weights = fix_bf16(weights)

  with Context(BEAM=0):
    # quantize
    if quantize is not None:
      weights = linear.quantize(weights, device)
      for _,v in weights.items(): v.realize()

    # shard
    if isinstance(device, tuple):
      for k,v in nn.state.get_state_dict(model).items():
        if 'scale' in k: v.shard_(device, axis=None)  # from quantized
        elif '.attention.' in k: v.shard_(device, axis=-1)
        elif '.feed_forward.w1.' in k: v.shard_(device, axis=0)
        elif '.feed_forward.w3.' in k: v.shard_(device, axis=0)
        elif '.feed_forward.' in k: v.shard_(device, axis=-1)
        elif 'tok_embeddings.weight' in k: v.shard_(device, axis=0)
        elif 'output.weight' in k: v.shard_(device, axis=0)
        else: v.shard_(device, axis=None)

    # replace weights in model
    load_state_dict(model, weights, strict=False, consume=True)
  return model

# default settings
TEMPERATURE = 0 # 0.85
TOP_K = 25
TOP_P = 0.9
ALPHA_F = 0.1
ALPHA_P = 0.0

def prefill(model, toks, start_pos=0):
  # prefill the model
  for tok in tqdm(toks):
    GlobalCounters.reset()
    model(Tensor([[tok]]), start_pos, TEMPERATURE, TOP_K, TOP_P, ALPHA_F, ALPHA_P).realize()
    start_pos += 1
  return start_pos

class TinygradDynamicShardInferenceEngine(InferenceEngine):
    def __init__(self):
        self.shard = None

    async def infer_prompt(self, shard: Shard, prompt: str, inference_state: Optional[str] = None) -> (np.ndarray, str, bool):
        await self.ensure_shard(shard)
        start_pos = json.loads(inference_state).get("start_pos", 0) if inference_state else 0

        toks = self.tokenizer.encode(prompt)
        start_pos = prefill(self.model, toks[:-1], start_pos=start_pos)
        last_tok = toks[-1]

        output_data = np.array([self.model(Tensor([[last_tok]]), start_pos, TEMPERATURE, TOP_K, TOP_P, ALPHA_F, ALPHA_P).tolist()])
        if output_data.size == 1:
           start_pos += 1

        return output_data, json.dumps({"start_pos": start_pos}), output_data.size == 1 and output_data.item() in self.tokenizer.stop_tokens

    async def infer_tensor(self, shard: Shard, input_data: np.ndarray, inference_state: Optional[str] = None) -> (np.ndarray, str, bool):
        await self.ensure_shard(shard)
        start_pos = json.loads(inference_state).get("start_pos", 0) if inference_state else 0

        output_data: np.ndarray = np.array([self.model(Tensor([input_data]), start_pos, TEMPERATURE, TOP_K, TOP_P, ALPHA_F, ALPHA_P).tolist()])
        if output_data.size == 1:
           start_pos += 1

        return output_data, json.dumps({"start_pos": start_pos}), output_data.size == 1 and output_data.item() in self.tokenizer.stop_tokens

    async def reset_shard(self, shard: Shard):
        await self.ensure_shard(shard)

        self.model.reset()

    async def ensure_shard(self, shard: Shard):
        if self.shard == shard:
            return

        model_path = Path(shard.model_id)
        models_dir = Path(_cache_dir) / "tinygrad" / "downloads"
        model_path = models_dir / shard.model_id
        size = "8B"
        if Path(model_path / "model.safetensors.index.json").exists():
            model = model_path
        else:

            if DEBUG >= 2: print(f"Downloading tinygrad model {shard.model_id}...")
            if shard.model_id.lower().find("llama3-8b-sfr") != -1:
                await fetch_async("https://huggingface.co/bofenghuang/Meta-Llama-3-8B/resolve/main/original/tokenizer.model", "tokenizer.model", subdir=shard.model_id)
                await fetch_async("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-8B-R/resolve/main/model-00001-of-00004.safetensors", "model-00001-of-00004.safetensors", subdir=shard.model_id)
                await fetch_async("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-8B-R/resolve/main/model-00002-of-00004.safetensors", "model-00002-of-00004.safetensors", subdir=shard.model_id)
                await fetch_async("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-8B-R/resolve/main/model-00003-of-00004.safetensors", "model-00003-of-00004.safetensors", subdir=shard.model_id)
                await fetch_async("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-8B-R/resolve/main/model-00004-of-00004.safetensors", "model-00004-of-00004.safetensors", subdir=shard.model_id)
                model = await fetch_async("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-8B-R/raw/main/model.safetensors.index.json", "model.safetensors.index.json", subdir=shard.model_id)
                size = "8B"
            elif shard.model_id.lower().find("llama3-70b-sfr") != -1:
                raise NotImplementedError("llama3-70b-sfr is not implemented for tinygrad")
                # fetch("https://huggingface.co/bofenghuang/Meta-Llama-3-70B/resolve/main/original/tokenizer.model", "tokenizer.model", subdir=shard.model_id)
                # fetch("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-70B-R/resolve/main/model-00001-of-00004.safetensors", "model-00001-of-00004.safetensors", subdir=shard.model_id)
                # fetch("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-70B-R/resolve/main/model-00002-of-00004.safetensors", "model-00002-of-00004.safetensors", subdir=shard.model_id)
                # fetch("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-70B-R/resolve/main/model-00003-of-00004.safetensors", "model-00003-of-00004.safetensors", subdir=shard.model_id)
                # fetch("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-70B-R/resolve/main/model-00004-of-00004.safetensors", "model-00004-of-00004.safetensors", subdir=shard.model_id)
                # model = fetch("https://huggingface.co/TriAiExperiments/SFR-Iterative-DPO-LLaMA-3-70B-R/raw/main/model.safetensors.index.json", "model.safetensors.index.json", subdir=shard.model_id)
                # size = "70B"
            else:
                raise ValueError(f"tinygrad doesnt currently support arbitrary model downloading. unsupported model: {shard.model_id}")

        model = build_transformer(model_path, shard=shard, model_size=size)
        tokenizer = Tokenizer(str((model_path if model_path.is_dir() else model_path.parent) / "tokenizer.model"))

        self.shard = shard
        self.model = model
        self.tokenizer = tokenizer
