import dataclasses
from dataclasses import dataclass
from functools import partial
import math
import os
from typing import Callable, Optional, Tuple, Dict

import alpa
from alpa import mark_pipeline
from alpa.model.model_util import ModelOutput
import flax.linen as nn
import jax
import flax
from jax import lax
import jax.numpy as jnp
from jax.tree_util import tree_flatten, tree_unflatten, tree_leaves
import jaxlib.xla_extension as jax_xla
import numpy as np
from tqdm import tqdm


ACT2FN = {
    "gelu": partial(nn.gelu, approximate=False),
    "relu": nn.relu,
    "silu": nn.swish,
    "swish": nn.swish,
    "gelu_new": partial(nn.gelu, approximate=True),
}


@flax.struct.dataclass
class OPTModelOutput(ModelOutput):
    last_hidden_state: jax_xla.DeviceArray
    hidden_states: Optional[Tuple[jax_xla.DeviceArray]] = None
    attentions: Optional[Tuple[jax_xla.DeviceArray]] = None
    attention_cache: Optional[Tuple[Tuple[jax_xla.DeviceArray]]] = None


@flax.struct.dataclass
class OPTLMOutput(ModelOutput):
    logits: jax_xla.DeviceArray
    hidden_states: Optional[Tuple[jax_xla.DeviceArray]] = None
    attentions: Optional[Tuple[jax_xla.DeviceArray]] = None
    attention_cache: Optional[Tuple[Tuple[jax_xla.DeviceArray]]] = None


@dataclass(frozen=True)
class OPTConfig:
    # Inherited from OPT
    decoder_layers: int = 12
    max_target_positions: int = 2048
    decoder_embed_dim: int = 768
    decoder_attention_heads: int = 12
    decoder_input_dim: int = 768
    decoder_ffn_embed_dim: int = 3072
    batch_size: int = 1
    pad: int = 1
    activation_fn: str = 'relu'
    fp16: bool = True
    use_stable_embedding: bool = False
    no_scale_embedding: bool = True
    decoder_learned_pos: bool = True
    decoder_normalize_before: bool = True
    share_decoder_input_output_embed: bool = True
    # Added
    version: int = 1
    vocab_size: int = 50272
    layer_norm_eps: float = 0.00001
    num_pp_stages: int = None


class OPTEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""

    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        assert not self.config.use_stable_embedding
        self.embed_scale = 1.0 if self.config.no_scale_embedding else math.sqrt(
            self.config.decoder_embed_dim)
        self.word_embeddings = nn.Embed(
            self.config.vocab_size,
            self.config.decoder_input_dim,
            dtype=self.dtype,
        )
        assert self.config.max_target_positions is not None
        assert self.config.decoder_learned_pos
        self.position_embeddings = nn.Embed(
            self.config.max_target_positions + self.config.pad + 1,
            self.config.decoder_embed_dim,
            dtype=self.dtype,
        )
        self.project_in_dim = nn.Dense(
            self.config.decoder_embed_dim,
            dtype=self.dtype,
        ) if self.config.decoder_input_dim != self.config.decoder_embed_dim else None

    def __call__(self,
                 input_ids,
                 position_ids):
        # Embed
        inputs_embeds = self.embed_scale * self.word_embeddings(input_ids.astype("i4"))
        if self.project_in_dim is not None:
            inputs_embeds = self.project_in_dim(inputs_embeds)
        position_embeds = self.position_embeddings(position_ids.astype("i4"))

        # Sum all embeddings
        hidden_states = inputs_embeds + position_embeds
        return hidden_states


class OPTSelfAttention(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        if self.config.decoder_embed_dim % self.config.decoder_attention_heads != 0:
            raise ValueError(
                f"`decoder_embed_dim`: {self.config.decoder_embed_dim} has to be a "
                f"multiple of `decoder_attention_heads`: {self.config.decoder_attention_heads}"
            )

        self.qvk_combined = nn.Dense(
            self.config.decoder_embed_dim * 3,
            dtype=self.dtype,
        )

    def __call__(self,
                 hidden_states,
                 output_attentions: bool = False,
                 attention_cache=None):
        head_dim = self.config.decoder_embed_dim // self.config.decoder_attention_heads

        qvk_combined_states = self.qvk_combined(hidden_states)
        qvk_combined_states = qvk_combined_states.reshape(
            qvk_combined_states.shape[:2] + (-1, 3))
        query_states, value_states, key_states = jnp.split(qvk_combined_states,
                                                           3,
                                                           axis=3)

        query_states = query_states.reshape(hidden_states.shape[:2] +
                                            (self.config.decoder_attention_heads,
                                             head_dim))
        value_states = value_states.reshape(hidden_states.shape[:2] +
                                            (self.config.decoder_attention_heads,
                                             head_dim))
        key_states = key_states.reshape(hidden_states.shape[:2] +
                                        (self.config.decoder_attention_heads,
                                         head_dim))

        if attention_cache is None:
            attention_bias = jnp.expand_dims(jnp.triu(jnp.full(
                (query_states.shape[1], key_states.shape[1]), -1e10), 1), (0, 1))
        else:
            cache_key, cache_value, cache_index = attention_cache
            key_states = lax.dynamic_update_slice(cache_key, key_states, (0, cache_index[0], 0, 0))
            value_states = lax.dynamic_update_slice(cache_value, value_states, (0, cache_index[0], 0, 0))
            num_updated_cache_vectors = query_states.shape[1]
            max_length = key_states.shape[1]
            attention_bias = (jnp.arange(max_length) >= cache_index + num_updated_cache_vectors).astype(self.dtype) * -1e10
            attention_bias = attention_bias[None, None, None, :]
            attention_cache = key_states, value_states, cache_index + num_updated_cache_vectors
        attn_weights = nn.attention.dot_product_attention_weights(
            query_states,
            key_states,
            bias=attention_bias,
            dtype=self.dtype,
            precision=None,
        )

        attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights,
                                 value_states)
        attn_output = attn_output.reshape(attn_output.shape[:2] + (-1,))

        outputs = (attn_output, attention_cache,
                   attn_weights) if output_attentions else (attn_output, attention_cache)
        return outputs


class OPTAttention(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        assert self.config.decoder_normalize_before
        self.self = OPTSelfAttention(self.config, dtype=self.dtype)
        self.dense = nn.Dense(
            self.config.decoder_embed_dim,
            dtype=self.dtype,
        )
        self.layer_norm = nn.LayerNorm(epsilon=self.config.layer_norm_eps,
                                       dtype=self.dtype)

    def __call__(self,
                 hidden_states,
                 output_attentions: bool = False,
                 attention_cache=None):
        residual = hidden_states
        hidden_states = self.layer_norm(hidden_states)
        attn_outputs = self.self(hidden_states,
                                 output_attentions=output_attentions,
                                 attention_cache=attention_cache)
        attn_output = attn_outputs[0]
        attention_cache = attn_outputs[1]
        hidden_states = self.dense(attn_output)
        hidden_states = hidden_states + residual
        outputs = (hidden_states, attention_cache)

        if output_attentions:
            outputs += (attn_outputs[2],)

        return outputs


class OPTFFN(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.fc1 = nn.Dense(
            self.config.decoder_ffn_embed_dim,
            dtype=self.dtype,
        )
        self.activation = ACT2FN[self.config.activation_fn]
        self.fc2 = nn.Dense(
            self.config.decoder_embed_dim,
            dtype=self.dtype,
        )
        self.layer_norm = nn.LayerNorm(epsilon=self.config.layer_norm_eps,
                                       dtype=self.dtype)

    def __call__(self, hidden_states):
        residual = hidden_states
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = self.activation(self.fc1(hidden_states))
        hidden_states = self.fc2(hidden_states)
        hidden_states = hidden_states + residual
        return hidden_states


class OPTTransformerLayer(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        assert self.config.decoder_normalize_before
        assert not getattr(self.config, "cross_self_attention", False)
        assert not getattr(self.config, "scale_heads", False)
        assert not getattr(self.config, "scale_attn", False)
        assert not getattr(self.config, "scale_fc", False)
        self.attention = OPTAttention(self.config, dtype=self.dtype)
        self.ffn = OPTFFN(self.config, dtype=self.dtype)

    def __call__(self,
                 hidden_states,
                 output_attentions: bool = False,
                 attention_cache=None):

        attention_outputs = self.attention(hidden_states,
                                           output_attentions=output_attentions,
                                           attention_cache=attention_cache)
        attention_output = attention_outputs[0]
        attention_cache = attention_outputs[1]

        hidden_states = self.ffn(attention_output)

        outputs = (hidden_states, attention_cache)

        if output_attentions:
            outputs += (attention_outputs[2],)
        return outputs


class OPTTransformerLayerCollection(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.layers = [
            OPTTransformerLayer(self.config,
                                name=str(i),
                                dtype=self.dtype)
            for i in range(self.config.decoder_layers)
        ]

    def __call__(
        self,
        hidden_states,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
        attention_cache=None,
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        new_attention_cache = () if attention_cache is not None else None

        if self.config.num_pp_stages is not None:
            assert self.config.decoder_layers % self.config.num_pp_stages == 0
            layers_per_stage = self.config.decoder_layers // self.config.num_pp_stages

        for i, layer in enumerate(self.layers):
            if self.config.num_pp_stages is not None:
                if i % layers_per_stage == 0 and i != 0:
                    stage_id = i // layers_per_stage
                    mark_pipeline(name=str(stage_id - 1), mark_type="end")
                    mark_pipeline(name=str(stage_id), mark_type="start")

            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            layer_attention_cache = None
            if attention_cache is not None:
                layer_attention_cache = attention_cache[i]
            layer_outputs = layer(hidden_states,
                                  output_attentions=output_attentions,
                                  attention_cache=layer_attention_cache)
            hidden_states = layer_outputs[0]
            if attention_cache is not None:
                new_attention_cache += (layer_outputs[1], )
            if output_attentions:
                all_attentions += (layer_outputs[2],)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        outputs = (hidden_states,)

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return OPTModelOutput(last_hidden_state=hidden_states,
                              hidden_states=all_hidden_states,
                              attentions=all_attentions,
                              attention_cache=new_attention_cache)


class OPTTransformerModule(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        assert self.config.decoder_normalize_before
        self.embeddings = OPTEmbeddings(self.config, dtype=self.dtype)
        self.encoder = OPTTransformerLayerCollection(self.config, dtype=self.dtype)
        if self.config.version > 2:
            self.layer_norm = nn.LayerNorm(epsilon=self.config.layer_norm_eps,
                                           dtype=self.dtype)

    def __call__(
        self,
        input_ids,
        position_ids,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
        attention_cache=None,
    ):
        hidden_states = self.embeddings(input_ids,
                                        position_ids)
        outputs = self.encoder(
            hidden_states,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            attention_cache=attention_cache,
        )
        hidden_states = outputs[0]
        if self.config.version > 2:
            hidden_states = self.layer_norm(hidden_states)

        if not return_dict:
            # if pooled is None, don't return it
            return (hidden_states,) + outputs[1:]

        return OPTModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            attention_cache=outputs.attention_cache,
        )


class OPTForLMModule(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32
    bias_init: Callable[..., jnp.ndarray] = jax.nn.initializers.zeros

    def setup(self):
        self.transformers = OPTTransformerModule(config=self.config,
                                                 dtype=self.dtype)

        self.project_out_dim = nn.Dense(
            self.config.decoder_input_dim,
            dtype=self.dtype,
        ) if self.config.decoder_input_dim != self.config.decoder_embed_dim else None

        if self.config.share_decoder_input_output_embed:
            self.decoder = None
        else:
            self.decoder = nn.Dense(self.config.vocab_size,
                                    dtype=self.dtype,
                                    use_bias=False)

    def __call__(
        self,
        input_ids,
        position_ids,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
        attention_cache=None,
    ):
        # Model
        outputs = self.transformers(
            input_ids,
            position_ids,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            attention_cache=attention_cache,
        )

        hidden_states = outputs[0]

        if self.project_out_dim is not None:
            hidden_states = self.project_out_dim(hidden_states)

        if self.config.share_decoder_input_output_embed:
            if self.dtype == jnp.float16:
                shared_embedding = self.transformers.embeddings.word_embeddings.embedding_fp16
            else:
                shared_embedding = self.transformers.variables["params"][
                    "embeddings"]["word_embeddings"]["embedding"]
            assert self.decoder is None
            logits = hidden_states @ shared_embedding.T
        else:
            assert self.decoder is not None
            logits = self.decoder(hidden_states)

        # Compute the prediction scores
        if not return_dict:
            return (logits,) + outputs[1:]

        return OPTLMOutput(
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            attention_cache=outputs.attention_cache,
        )


def get_config(name, **kwargs):
    if name == "125M":
        config = OPTConfig(
            decoder_layers=12,
            max_target_positions=2048,
            decoder_embed_dim=768,
            decoder_attention_heads=12,
            decoder_input_dim=768,
            decoder_ffn_embed_dim=3072,
        )
    elif name == "30B":
        config = OPTConfig(
            decoder_layers=48,
            max_target_positions=2048,
            decoder_embed_dim=7168,
            decoder_attention_heads=56,
            decoder_input_dim=7168,
            decoder_ffn_embed_dim=28672,
            version=3,
        )
    elif name == "175B":
        config = OPTConfig(
            decoder_layers=96,
            max_target_positions=2048,
            decoder_embed_dim=12288,
            decoder_attention_heads=96,
            decoder_input_dim=12288,
            decoder_ffn_embed_dim=49152,
            version=3,
        )
    else:
        raise ValueError()

    return dataclasses.replace(config, **kwargs)


def init_model_aval(config):
    model = OPTForLMModule(config)
    rngkey = jax.core.ShapedArray((2,), jnp.uint32)
    input_ids = jax.core.ShapedArray((1, 128), jnp.int32)
    position_ids = jax.core.ShapedArray((1, 128), jnp.int32)
    params = jax.eval_shape(model.init, rngkey, input_ids, position_ids)

    if config.fp16:
        params = jax.tree_map(lambda x: jax.ShapeDtypeStruct(x.shape, jnp.float16), params)

    return model, params


def build_init_cache_aval(config):
    batch_size = config.batch_size
    dtype = jnp.float32
    head_dim = config.decoder_embed_dim // config.decoder_attention_heads

    all_cache = []
    for i in range(config.decoder_layers):
        layer_cache = (
            jax.core.ShapedArray((batch_size, config.max_target_positions,
                                  config.decoder_attention_heads, head_dim),
                                  dtype),
            jax.core.ShapedArray((batch_size, config.max_target_positions,
                                  config.decoder_attention_heads, head_dim),
                                  dtype),
            jax.core.ShapedArray((batch_size,), jnp.int32),
        )
        all_cache.append(layer_cache)
    return tuple(all_cache)


def build_init_cache(config):
    batch_size = config.batch_size
    dtype = jnp.float32
    head_dim = config.decoder_embed_dim // config.decoder_attention_heads

    all_cache = []
    for i in range(config.decoder_layers):
        layer_cache = (
            jnp.zeros((batch_size, config.max_target_positions,
                       config.decoder_attention_heads, head_dim),
                       dtype=dtype),
            jnp.zeros((batch_size, config.max_target_positions,
                       config.decoder_attention_heads, head_dim),
                       dtype=dtype),
            jnp.full((batch_size,), 0, jnp.int32),
        )
        all_cache.append(layer_cache)
    return tuple(all_cache)


def build_position_ids(input_ids, padding_idx):
    mask = (input_ids != padding_idx).astype(jnp.int32)
    position_ids = jnp.cumsum(mask, axis=1).astype(jnp.int32) * mask + padding_idx
    return position_ids


def inference_step_no_cache(params, batch, apply_func):
    logits = apply_func(params,
                        batch["input_ids"],
                        batch["position_ids"])[0]
    return logits


def load_np_params(params, path, config, dummy=False):
    def load_array(key):
        if dummy:
            return np.ones((1,))
        return np.load(os.path.join(path, key))

    def load_param(param_key, loaded_array):
        param_dict = params
        param_keys = param_key.split('.')
        for i, key in enumerate(param_keys):
            if i == len(param_keys) - 1:
                if dummy:
                    param_dict[key] = jax.core.ShapedArray(
                        param_dict[key].shape, param_dict[key].dtype)
                else:
                    assert param_dict[key].shape == loaded_array.shape
                    assert param_dict[key].dtype == loaded_array.dtype
                    param_dict[key] = loaded_array
            else:
                param_dict = param_dict[key]

    params = params.unfreeze()
    load_param("params.transformers.embeddings.word_embeddings.embedding",
               load_array("decoder.embed_tokens.weight"))
    load_param("params.transformers.embeddings.position_embeddings.embedding",
               load_array("decoder.embed_positions.weight"))
    if config.version > 2:
        load_param("params.transformers.layer_norm.scale",
                   load_array("decoder.layer_norm.weight"))
        load_param("params.transformers.layer_norm.bias",
                   load_array("decoder.layer_norm.bias"))
    for i in tqdm(range(config.decoder_layers)):
        param_prefix = f"params.transformers.encoder.{i}."
        load_prefix = f"decoder.layers.{i}."
        # Attention weights
        wq = load_array(load_prefix + "self_attn.q_proj.weight")
        wk = load_array(load_prefix + "self_attn.k_proj.weight")
        wv = load_array(load_prefix + "self_attn.v_proj.weight")
        dim = wq.shape[-1]
        w_qvk = np.concatenate([wq, wv, wk], axis=0).reshape((3, -1, dim)).transpose([2, 1, 0]).reshape((dim, -1))
        load_param(param_prefix + "attention.self.qvk_combined.kernel", w_qvk)
        bq = load_array(load_prefix + "self_attn.q_proj.bias")
        bk = load_array(load_prefix + "self_attn.k_proj.bias")
        bv = load_array(load_prefix + "self_attn.v_proj.bias")
        b_qvk = np.concatenate([bq, bv, bk], axis=0).reshape((3, dim)).transpose([1, 0]).reshape((-1,))
        load_param(param_prefix + "attention.self.qvk_combined.bias", b_qvk)
        load_param(param_prefix + "attention.dense.kernel",
                   np.transpose(load_array(load_prefix + "self_attn.out_proj.weight")))
        load_param(param_prefix + "attention.dense.bias",
                   load_array(load_prefix + "self_attn.out_proj.bias"))
        load_param(param_prefix + "attention.layer_norm.scale",
                   load_array(load_prefix + "self_attn_layer_norm.weight"))
        load_param(param_prefix + "attention.layer_norm.bias",
                   load_array(load_prefix + "self_attn_layer_norm.bias"))
        # FFN weights
        load_param(param_prefix + "ffn.fc1.bias",
                   load_array(load_prefix + "fc1.bias"))
        load_param(param_prefix + "ffn.fc1.kernel",
                   np.transpose(load_array(load_prefix + "fc1.weight")))
        load_param(param_prefix + "ffn.fc2.bias",
                   load_array(load_prefix + "fc2.bias"))
        load_param(param_prefix + "ffn.fc2.kernel",
                   np.transpose(load_array(load_prefix + "fc2.weight")))
        load_param(param_prefix + "ffn.layer_norm.scale",
                   load_array(load_prefix + "final_layer_norm.weight"))
        load_param(param_prefix + "ffn.layer_norm.bias",
                   load_array(load_prefix + "final_layer_norm.bias"))

    return flax.core.freeze(params)


def get_pipeshard_executable(config):
    # Init model
    model, params = init_model_aval(config)

    # Parallelize
    method = alpa.PipeshardParallel(num_micro_batches=1,
                                    pipeline_schedule="inference")

    @alpa.parallelize(batch_argnums=(1,), method=method)
    def inference_step_with_cache(params, batch):
        @alpa.manual_layer_construction
        def forward(params):
            alpa.mark_pipeline(name="0", mark_type="start")
            output = model.apply(params,
                                 batch["input_ids"],
                                 batch["position_ids"],
                                 attention_cache=batch["cache"])
            alpa.mark_pipeline(name=f"{config.num_pp_stages - 1}", mark_type="end")
            return output

        output = forward(params)
        return output.logits, output.attention_cache

    executable = inference_step_with_cache.get_executable(params, {
        "input_ids": jax.core.ShapedArray((1, 1), jnp.int32),
        "position_ids": jax.core.ShapedArray((1, 1), jnp.int32),
        "cache": build_init_cache_aval(config),
    })

    return executable, params


def load_distributed_params(path, executable, params_aval, config):
    if path[-2:] == "np":
        params_info, _ = executable.get_load_info()
        params = load_np_params(params_aval, path, config)
        flat_args, in_tree = tree_flatten(params)
        flat_info = tree_leaves(params_info)
        return executable.mesh_group.shard_args_to_arrays(flat_info, flat_args)
    elif path[-2:] == "ts":
        params_info, _ = executable.get_load_info()
        return alpa.restore_checkpoint(path, 1, params_info, params_info)
    else:
        raise ValueError()
