from copy import deepcopy
from itertools import count
from typing import Any, List, Optional, Set

import torch as t
from transformer_lens import HookedTransformer

from auto_circuit.data import PromptDataLoader
from auto_circuit.model_utils.sparse_autoencoder import (
    SparseAutoencoder,
    load_autoencoder,
)
from auto_circuit.types import AutoencoderInput, DestNode, SrcNode
from auto_circuit.utils.custom_tqdm import tqdm
from auto_circuit.utils.patchable_model import PatchableModel


class AutoencoderTransformer(t.nn.Module):
    wrapped_model: t.nn.Module
    sparse_autoencoders: List[SparseAutoencoder]

    def __init__(self, wrapped_model: t.nn.Module, saes: List[SparseAutoencoder]):
        super().__init__()
        self.sparse_autoencoders = saes

        if isinstance(wrapped_model, PatchableModel):
            self.wrapped_model = wrapped_model.wrapped_model
        else:
            self.wrapped_model = wrapped_model

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model(*args, **kwargs)

    def _prune_latents_with_dataset(
        self,
        dataloader: PromptDataLoader,
        max_latents: Optional[int],
        include_corrupt: bool = False,
        seq_len: Optional[int] = None,
    ):
        """
        !In place operation!
        Prune the weights of the autoencoder to remove latents that are never activated
        by the dataset. This can reduce the number of edges in the factorized model by a
        factor of 10 or more.
        """
        for sae in self.sparse_autoencoders:
            sae.reset_activated_latents(seq_len=seq_len)

        print("Running dataset for autoencoder pruning...")
        unpruned_logits = []
        for batch_idx, batch in (batch_pbar := tqdm(enumerate(dataloader))):
            batch_pbar.set_description_str(f"Pruning Autoencoder: Batch {batch_idx}")
            for input_idx, prompt in (input_pbar := tqdm(enumerate(batch.clean))):
                input_pbar.set_description_str(f"Clean Batch Input {input_idx}")
                with t.inference_mode():
                    out = self.forward(prompt.unsqueeze(0))  # Run one at a time
                unpruned_logits.append(out)
            if include_corrupt:
                for input_idx, prompt in (input_pbar := tqdm(enumerate(batch.corrupt))):
                    input_pbar.set_description_str(f"Corrupt Batch Input {input_idx}")
                    with t.inference_mode():
                        out = self.forward(prompt.unsqueeze(0))
                    unpruned_logits.append(out)

        activated_latent_counts, latent_counts = [], []
        for sae in self.sparse_autoencoders:
            activated = (sae.latent_total_act > 0).sum(dim=-1).tolist()
            activated_latent_counts.append(activated)
            activated_count = activated if type(activated) == int else max(activated)
            max_latents = max_latents or activated_count
            latent_counts.append(max_idx := min(max_latents, activated_count))
            sorted_latents = t.sort(sae.latent_total_act, dim=-1, descending=True)
            idxs_to_keep = sorted_latents.indices[..., :max_idx]
            sae.prune_latents(idxs_to_keep)

        pruned_logits = []
        with t.inference_mode():
            for batch_idx, batch in (batch_pbar := tqdm(enumerate(dataloader))):
                batch_pbar_str = f"Testing Pruned Autoencoder: Batch {batch_idx}"
                batch_pbar.set_description_str(batch_pbar_str)
                out = self.forward(batch.clean)
                pruned_logits.append(out)
                if include_corrupt:
                    out = self.forward(batch.corrupt)
                    pruned_logits.append(out)

        flat_pruned_logits = t.flatten(t.stack(pruned_logits), end_dim=-2)
        flat_unpruned_logits = t.flatten(t.stack(unpruned_logits), end_dim=-2)
        kl_div = t.nn.functional.kl_div(
            t.nn.functional.log_softmax(flat_pruned_logits, dim=-1),
            t.nn.functional.log_softmax(flat_unpruned_logits, dim=-1),
            reduction="batchmean",
            log_target=True,
        )

        print("Done. Autoencoder activated latent counts:", activated_latent_counts)
        print("Autoencoder latent counts:", latent_counts)
        print("Pruned vs. Unpruned KL Div:", kl_div.item())

    def run_with_cache(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model.run_with_cache(*args, **kwargs)

    def add_hook(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model.add_hook(*args, **kwargs)

    @property
    def cfg(self) -> Any:
        return self.wrapped_model.cfg

    @property
    def tokenizer(self) -> Any:
        return self.wrapped_model.tokenizer

    @property
    def input_to_embed(self) -> Any:
        return self.wrapped_model.input_to_embed

    @property
    def blocks(self) -> Any:
        return self.wrapped_model.blocks

    def to_tokens(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model.to_tokens(*args, **kwargs)

    def to_str_tokens(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model.to_str_tokens(*args, **kwargs)

    def to_string(self, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped_model.to_string(*args, **kwargs)

    def __str__(self) -> str:
        return self.wrapped_model.__str__()

    def __repr__(self) -> str:
        return self.wrapped_model.__repr__()


def autoencoder_model(
    model: HookedTransformer,
    sae_input: AutoencoderInput = "resid_delta_mlp",
    pythia_size: Optional[str] = None,
    new_instance: bool = True,
) -> AutoencoderTransformer:
    if new_instance:
        model = deepcopy(model)
    sparse_autoencoders: List[SparseAutoencoder] = []
    for layer_idx in range(model.cfg.n_layers):
        if sae_input == "mlp_post_act":
            hook_point = model.blocks[layer_idx].mlp.hook_post
            sae = load_autoencoder(hook_point, model, layer_idx, sae_input, pythia_size)
            setattr(model.blocks[layer_idx].mlp, "hook_post", sae_input)
        else:
            assert sae_input == "resid_delta_mlp"
            hook_point = model.blocks[layer_idx].hook_mlp_out
            sae = load_autoencoder(hook_point, model, layer_idx, sae_input, pythia_size)
            setattr(model.blocks[layer_idx], "hook_mlp_out", sae)
        sae.to(model.cfg.device)
        sparse_autoencoders.append(sae)
    return AutoencoderTransformer(model, sparse_autoencoders)


def factorized_src_nodes(model: AutoencoderTransformer) -> Set[SrcNode]:
    """Get the source part of each edge in the factorized graph, grouped by layer.
    Graph is factorized following the Mathematical Framework paper."""
    assert model.cfg.use_attn_result  # Get attention head outputs separately
    assert (
        model.cfg.use_attn_in
    )  # Get attention head inputs separately (but Q, K, V are still combined)
    assert model.cfg.use_split_qkv_input  # Separate Q, K, V input for each head
    assert model.cfg.use_hook_mlp_in  # Get MLP input BEFORE layernorm
    layers, idxs = count(), count()
    nodes = set()
    nodes.add(
        SrcNode(
            name="Resid Start",
            module_name="blocks.0.hook_resid_pre",
            layer=next(layers),
            idx=next(idxs),
            weight="embed.W_E",
        )
    )

    for block_idx in range(model.cfg.n_layers):
        layer = next(layers)
        for head_idx in range(model.cfg.n_heads):
            nodes.add(
                SrcNode(
                    name=f"A{block_idx}.{head_idx}",
                    module_name=f"blocks.{block_idx}.attn.hook_result",
                    layer=layer,
                    idx=next(idxs),
                    head_dim=2,
                    head_idx=head_idx,
                    weight=f"blocks.{block_idx}.attn.W_O",
                    weight_head_dim=0,
                )
            )
        layer = layer if model.cfg.parallel_attn_mlp else next(layers)
        for latent_idx in range(model.blocks[block_idx].hook_mlp_out.n_latents):
            nodes.add(
                SrcNode(
                    name=f"MLP {block_idx} Latent {latent_idx}",
                    module_name=f"blocks.{block_idx}.hook_mlp_out.latent_outs",
                    layer=layer,
                    idx=next(idxs),
                    head_dim=2,
                    head_idx=latent_idx,
                    weight=f"blocks.{block_idx}.hook_mlp_out.decoder.weight",
                    weight_head_dim=0,
                )
            )
    return nodes


def factorized_dest_nodes(model: AutoencoderTransformer) -> Set[DestNode]:
    """Get the destination part of each edge in the factorized graph, grouped by layer.
    Graph is factorized following the Mathematical Framework paper."""
    assert model.cfg.use_attn_result  # Get attention head outputs separately
    assert (
        model.cfg.use_attn_in
    )  # Get attention head inputs separately (but Q, K, V are still combined)
    # assert model.cfg.use_split_qkv_input  # Separate Q, K, V input for each head
    assert model.cfg.use_hook_mlp_in  # Get MLP input BEFORE layernorm
    layers, idxs = count(1), count()
    nodes = set()
    for block_idx in range(model.cfg.n_layers):
        layer = next(layers)
        for head_idx in range(model.cfg.n_heads):
            nodes.add(
                DestNode(
                    name=f"A{block_idx}.{head_idx}",
                    module_name=f"blocks.{block_idx}.hook_attn_in",
                    layer=layer,
                    idx=next(idxs),
                    head_dim=2,
                    head_idx=head_idx,
                )
            )
        nodes.add(
            DestNode(
                name=f"MLP {block_idx}",
                module_name=f"blocks.{block_idx}.hook_mlp_in",
                layer=layer if model.cfg.parallel_attn_mlp else next(layers),
                idx=next(idxs),
                weight=f"blocks.{block_idx}.mlp.W_in",
            )
        )
    nodes.add(
        DestNode(
            name="Resid End",
            module_name=f"blocks.{model.cfg.n_layers - 1}.hook_resid_post",
            layer=next(layers),
            idx=next(idxs),
            weight="unembed.W_U",
        )
    )
    return nodes