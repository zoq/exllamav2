import sys
min_version = (3, 9)
if sys.version_info < min_version:
    print("")
    print(f" ## Warning: this project requires Python {min_version[0]}.{min_version[1]} or higher.")
    print("")

import torch
from torch import nn
import torch.nn.functional as F
from safetensors import safe_open
import math
from exllamav2.config import ExLlamaV2Config
from exllamav2.cache import ExLlamaV2Cache
from exllamav2.linear import ExLlamaV2Linear
from exllamav2.module import ExLlamaV2Module
from exllamav2.rmsnorm import ExLlamaV2RMSNorm
from exllamav2.attn import ExLlamaV2Attention
from exllamav2.mlp import ExLlamaV2MLP
from exllamav2.embedding import ExLlamaV2Embedding

def _torch_device(idx):
    if idx == -1: return "cpu"
    return f"cuda:{idx}"


class ExLlamaV2DeviceTensors:

    model = None
    device_idx: int
    ready: bool

    scratch_bytes: int
    scratch_idx: int

    sin: torch.tensor
    cos: torch.tensor

    scratch: torch.tensor = None


    def __init__(self, model, device_idx, scratch_bytes):

        self.model = model
        self.device_idx = device_idx
        self.ready = False
        self.scratch_bytes = scratch_bytes
        self.scratch_idx = 0


    def prepare(self, scratch):

        self.prepare_sincos()

        if scratch:
            self.scratch = torch.empty((self.scratch_bytes // 2,), dtype = torch.half, device = _torch_device(self.device_idx))

        self.ready = True


    def begin_scratch_alloc(self):

        self.scratch_idx = 0


    def get_scratch_slice(self, size_bytes):

        if self.scratch is None: self.prepare(True)

        size_bytes = ((size_bytes + 127) // 128) * 128
        size_half = size_bytes // 2
        scratch_slice = self.scratch.narrow(0, self.scratch_idx, size_half)
        self.scratch_idx += size_half
        return scratch_slice


    def prepare_sincos(self):

        base = self.model.config.rotary_embedding_base
        alpha = self.model.config.scale_alpha_value
        scale = self.model.config.scale_pos_emb
        head_dim = self.model.config.head_dim
        device = _torch_device(self.device_idx)

        if alpha != 1.0: base *= alpha ** (self.model.config.head_dim / (self.model.config.head_dim - 2))

        inv_freq = 1.0 / (base ** (torch.arange(0, head_dim, 2, device = device).float() / head_dim))
        t = torch.arange(self.model.config.max_seq_len, device = device, dtype = torch.float32)

        if scale != 1.0: t /= scale

        freqs = torch.einsum("i,j->ij", t, inv_freq)
        emb = torch.cat((freqs, freqs), dim=-1)

        self.sin = emb.sin()[None, None, :, :].half()
        self.cos = emb.cos()[None, None, :, :].half()


class ExLlamaV2:

    config: ExLlamaV2Config
    modules: list = []
    modules_dict: dict = {}
    device_tensors: list = []
    cache_map: dict
    last_kv_layer_idx: int


    def __init__(self, config: ExLlamaV2Config, lazy_load = False):

        self.config = config
        self.modules = []
        self.modules_dict = {}
        self.device_tensors = []
        self.cache_map = {}

        # Build model

        self.modules.append(ExLlamaV2Embedding(self, "model.embed_tokens"))
        self.modules_dict[self.modules[-1].key] = self.modules[-1]

        for layer_idx in range(self.config.num_hidden_layers):

            self.modules.append(ExLlamaV2Attention(self, f"model.layers.{layer_idx}", layer_idx))
            for m in self.modules[-1].submodules: self.modules_dict[m.key] = m
            self.modules.append(ExLlamaV2MLP(self, f"model.layers.{layer_idx}", layer_idx))
            for m in self.modules[-1].submodules: self.modules_dict[m.key] = m

        self.modules.append(ExLlamaV2RMSNorm(self, "model.norm"))
        self.modules_dict[self.modules[-1].key] = self.modules[-1]
        self.modules.append(ExLlamaV2Linear(self, "lm_head", self.config.hidden_size, self.config.vocab_size, False))
        self.modules_dict[self.modules[-1].key] = self.modules[-1]

        # Find last layer that affects k/v cache

        layer_idx = len(self.modules)
        while True:
            layer_idx -= 1
            if isinstance(self.modules[layer_idx], ExLlamaV2Attention):
                break

        self.last_kv_layer_idx = layer_idx


    def set_device_map(self, allocation, embed_cpu = True):

        self.cache_map = {}

        sincos_size = self.config.head_dim * self.config.max_seq_len * 2
        constant_size = sincos_size * 2                                                                     # Constants shared between layers

        state_size = self.config.hidden_size * self.config.max_input_len * self.config.max_batch_size * 2   # Max size of hidden state
        mask_size = self.config.max_input_len ** 2 * 2

        allocation_bytes = [a * 1024**3 - state_size - mask_size - constant_size for a in allocation]       # Bytes remaining per device
        reserve_bytes = [0 for a in allocation]                                                             # Scratch space required per device

        current_idx = 0
        for idx, module in enumerate(self.modules):

            # Special case for token embeddings on CPU

            if idx == 0 and embed_cpu:

                module.set_device_idx(-1)
                continue

            # Advance current_idx until module fits in allocation

            footprint = module.weight_footprint()   # Footprint, in bytes
            scratch = module.scratch_space()        # Scratch space required by module

            while True:
                assert current_idx < len(allocation_bytes), "Insufficient space in device allocation"
                dev_scratch = max(scratch, reserve_bytes[current_idx])
                if footprint + dev_scratch <= allocation_bytes[current_idx]: break
                current_idx += 1

            # Subtract module size from allocation

            reserve_bytes[current_idx] = dev_scratch
            allocation_bytes[current_idx] -= footprint

            module.set_device_idx(current_idx)

        # Prepare to prepare device tensors

        self.device_tensors = []
        for idx, scratch_bytes in enumerate(reserve_bytes):
            self.device_tensors.append(ExLlamaV2DeviceTensors(self, idx, scratch_bytes))

        # Create map for cache

        self.set_cache_map()

        # Return unused space, in GB

        return [ab / 1024**3 for ab in allocation_bytes]


    def load(self, gpu_split = None, lazy = False):

        self.set_device_map(gpu_split or [99999])

        # Load module weights

        if not lazy:

            for module in self.modules: module.load()

        # Cache map

        self.set_cache_map()

        return gpu_split


    def set_cache_map(self):

        for module in self.modules:
            if isinstance(module, ExLlamaV2Attention): self.cache_map[module.layer_idx] = module.device()


    def create_device_tensors(self, scratch_bytes):

        for idx, bytes in enumerate(scratch_bytes):

            tensors = ExLlamaV2DeviceTensors(self, idx, bytes)
            self.device_tensors.append(tensors)


    def get_device_tensors(self, device_idx, scratch = True):

        tensors = self.device_tensors[device_idx]
        if not tensors.ready: tensors.prepare(scratch)
        return tensors


    def get_modules(self):

        return [module for module in self.modules]


    def build_attn_mask(self, batch_size, seq_len, past_len, input_mask, device):

        if seq_len > 1:

            attn_mask = torch.zeros(batch_size, 1, seq_len, past_len + seq_len, dtype = torch.float16, device = device)
            attn_mask_triu = torch.triu(torch.full((seq_len - 1, seq_len - 1), -65504.))
            attn_mask[:, :, : seq_len - 1, past_len + 1: past_len + seq_len] = attn_mask_triu

            if input_mask is not None:
                input_mask = torch.where(input_mask, 0, -65504.).half()
                input_mask = input_mask.unsqueeze(1).unsqueeze(2)
                attn_mask = torch.minimum(attn_mask, input_mask)

        else:

            attn_mask = None

        return attn_mask


    def forward(self, input_ids, cache = None, input_mask = None, preprocess_only = False):

        assert input_mask is None or input_mask.shape == input_ids.shape

        batch_size, seq_len = input_ids.shape
        past_len = 0 if cache is None else cache.current_seq_len

        x = input_ids
        prev_device = None
        attn_mask = None

        for idx, module in enumerate(self.modules):

            device = _torch_device(module.device_idx)

            # Build attention mask

            if device != prev_device and device != "cpu":

                prev_device = device
                attn_mask = self.build_attn_mask(batch_size, seq_len, past_len, input_mask, device)

            # Onward

            x = x.to(device)
            x = module.forward(x, cache, attn_mask)

            if preprocess_only and idx == self.last_kv_layer_idx:
                x = None
                break

            # print(module.key, module.name, x[0, 0])
            # print("max", torch.max(x).item(), "min",torch.min(x).item())

        # Advance cache

        if cache is not None:
            cache.current_seq_len += seq_len

        return x