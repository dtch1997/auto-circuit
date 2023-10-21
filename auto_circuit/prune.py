from collections import defaultdict
from typing import Dict, List

import torch as t
from torch.utils.data import DataLoader

from auto_circuit.data import PromptPairBatch
from auto_circuit.types import (
    ActType,
    Edge,
    ExperimentType,
    SrcNode,
)
from auto_circuit.utils.custom_tqdm import tqdm
from auto_circuit.utils.graph_utils import get_sorted_src_outs, patch_mode
from auto_circuit.visualize import draw_seq_graph


def run_pruned(
    model: t.nn.Module,
    data_loader: DataLoader[PromptPairBatch],
    experiment_type: ExperimentType,
    test_edge_counts: List[int],
    prune_scores: Dict[Edge, float],
    render_graph: bool = False,
    render_patched_edge_only: bool = False,
) -> Dict[int, List[t.Tensor]]:
    out_slice = model.out_slice
    pruned_outs: Dict[int, List[t.Tensor]] = defaultdict(list)
    prune_scores = dict(sorted(prune_scores.items(), key=lambda x: x[1], reverse=True))

    for batch_idx, batch in enumerate(batch_pbar := tqdm(data_loader)):
        batch_pbar.set_description_str(f"Pruning Batch {batch_idx}", refresh=True)
        if experiment_type.input_type == ActType.CLEAN:
            batch_input = batch.clean
        elif experiment_type.input_type == ActType.CORRUPT:
            batch_input = batch.corrupt
        else:
            raise NotImplementedError

        if 0 in test_edge_counts:
            with t.inference_mode():
                pruned_outs[0].append(model(batch_input)[out_slice])

        patch_outs: Dict[SrcNode, t.Tensor]
        if experiment_type.patch_type == ActType.CLEAN:
            patch_outs = get_sorted_src_outs(model, batch.clean)
        elif experiment_type.patch_type == ActType.CORRUPT:
            patch_outs = get_sorted_src_outs(model, batch.corrupt)
        else:
            assert experiment_type.patch_type == ActType.ZERO
            patch_outs = get_sorted_src_outs(model, batch.clean)
            patch_outs = dict([(n, t.zeros_like(out)) for n, out in patch_outs.items()])

        patch_src_outs: t.Tensor = t.stack(list(patch_outs.values())).detach()
        curr_src_outs: t.Tensor = t.zeros_like(patch_src_outs)

        with patch_mode(model, curr_src_outs, patch_src_outs, reset_mask=True):
            for edge_idx, edge in enumerate(edge_pbar := tqdm(prune_scores.keys())):
                edge_pbar.set_description(f"Prune Edge {edge}", refresh=False)
                n_edge = edge_idx + 1
                edge.patch_mask(model).data[edge.patch_idx] = 1
                if n_edge in test_edge_counts:
                    with t.inference_mode():
                        model_output = model(batch_input)
                    pruned_outs[n_edge].append(model_output[out_slice].detach().clone())
            if render_graph:
                labels = dict([(e, patch_outs[e.src]) for e, _ in prune_scores.items()])
                # draw_graph(model, batch_input, labels)
                draw_seq_graph(model, batch_input, labels, render_patched_edge_only)
        del patch_outs, patch_src_outs, curr_src_outs  # Free up memory
    return pruned_outs