from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
import trimesh
from torch import nn
from transformers import (
    AutoTokenizer,
    PreTrainedModel,
    Qwen2ForCausalLM,
    Qwen2Model,
)
from transformers.modeling_outputs import CausalLMOutputWithPast


class FourierPointEncoder(nn.Module):
    def __init__(self, hidden_size: int) -> None:
        super().__init__()
        frequencies = 2.0 ** torch.arange(8, dtype=torch.float32)
        self.register_buffer("frequencies", frequencies, persistent=False)
        self.projection = nn.Linear(51, hidden_size)

    def forward(self, points: torch.Tensor) -> torch.Tensor:
        encoded = (points.unsqueeze(-1) * self.frequencies).view(
            *points.shape[:-1],
            -1,
        )
        encoded = torch.cat((points, encoded.sin(), encoded.cos()), dim=-1)
        return self.projection(encoded)


class CADRecode(Qwen2ForCausalLM):
    def __init__(self, config: object) -> None:
        PreTrainedModel.__init__(self, config)
        self.model = Qwen2Model(config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(
            config.hidden_size,
            config.vocab_size,
            bias=False,
        )
        original_dtype = torch.get_default_dtype()
        torch.set_default_dtype(torch.float32)
        self.point_encoder = FourierPointEncoder(config.hidden_size)
        torch.set_default_dtype(original_dtype)

    def forward(
        self,
        input_ids: torch.Tensor | None = None,
        attention_mask: torch.Tensor | None = None,
        point_cloud: torch.Tensor | None = None,
        position_ids: torch.Tensor | None = None,
        past_key_values: object | None = None,
        inputs_embeds: torch.Tensor | None = None,
        labels: torch.Tensor | None = None,
        use_cache: bool | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
        return_dict: bool | None = None,
        cache_position: torch.Tensor | None = None,
    ) -> CausalLMOutputWithPast | tuple[torch.Tensor, ...]:
        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict
            if return_dict is not None
            else self.config.use_return_dict
        )
        if past_key_values is None or past_key_values.get_seq_length() == 0:
            if inputs_embeds is not None or point_cloud is None:
                raise ValueError("Initial decoding requires a point cloud")
            inputs_embeds = self.model.embed_tokens(input_ids)
            point_embeds = self.point_encoder(point_cloud).to(inputs_embeds.dtype)
            inputs_embeds[attention_mask == -1] = point_embeds.reshape(
                -1,
                point_embeds.shape[2],
            )
            attention_mask = attention_mask.clone()
            attention_mask[attention_mask == -1] = 1
            input_ids = None
            position_ids = None
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            cache_position=cache_position,
        )
        hidden_states = outputs[0]
        logits = self.lm_head(hidden_states).float()
        if not return_dict:
            return (logits, *outputs[1:])
        return CausalLMOutputWithPast(
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(self, *args: object, **kwargs: object) -> dict:
        model_inputs = super().prepare_inputs_for_generation(*args, **kwargs)
        model_inputs["point_cloud"] = kwargs["point_cloud"]
        return model_inputs


def mesh_to_point_cloud(
    mesh: trimesh.Trimesh,
    count: int = 256,
    preliminary_count: int = 8192,
) -> np.ndarray:
    sampled, _ = trimesh.sample.sample_surface(
        mesh,
        preliminary_count,
        seed=0,
    )
    selected = np.empty(count, dtype=np.int64)
    selected[0] = int(np.argmax(np.linalg.norm(sampled, axis=1)))
    minimum_squared = np.full(len(sampled), np.inf)
    for index in range(1, count):
        difference = sampled - sampled[selected[index - 1]]
        minimum_squared = np.minimum(
            minimum_squared,
            np.einsum("ij,ij->i", difference, difference),
        )
        selected[index] = int(np.argmax(minimum_squared))
    return np.asarray(sampled[selected], dtype=np.float32)


def generate_code(
    stl_path: Path,
    model_name: str,
    max_new_tokens: int,
) -> str:
    loaded = trimesh.load(stl_path, force="mesh", process=True)
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("Input did not load as a triangle mesh")
    loaded.apply_translation(-(loaded.bounds[0] + loaded.bounds[1]) / 2)
    loaded.apply_scale(2 / max(loaded.extents))
    point_cloud = mesh_to_point_cloud(loaded)

    tokenizer = AutoTokenizer.from_pretrained(
        "Qwen/Qwen2-1.5B",
        pad_token="<|im_end|>",
        padding_side="left",
    )
    model = CADRecode.from_pretrained(
        model_name,
        torch_dtype="auto",
    ).eval()
    input_ids = [tokenizer.pad_token_id] * len(point_cloud) + [
        tokenizer("<|im_start|>")["input_ids"][0]
    ]
    attention_mask = [-1] * len(point_cloud) + [1]
    with torch.no_grad():
        batch_ids = model.generate(
            input_ids=torch.tensor(input_ids).unsqueeze(0),
            attention_mask=torch.tensor(attention_mask).unsqueeze(0),
            point_cloud=torch.tensor(point_cloud).unsqueeze(0),
            max_new_tokens=max_new_tokens,
            pad_token_id=tokenizer.pad_token_id,
        )
    decoded = tokenizer.batch_decode(batch_ids)[0]
    begin = decoded.find("<|im_start|>") + len("<|im_start|>")
    end = decoded.find("<|endoftext|>", begin)
    return decoded[begin : end if end >= 0 else None].strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate CadQuery code with the CAD-Recode fallback model."
    )
    parser.add_argument("stl", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model",
        default="filapro/cad-recode-v1.5",
    )
    parser.add_argument("--max-new-tokens", type=int, default=768)
    arguments = parser.parse_args()
    code = generate_code(
        arguments.stl,
        arguments.model,
        arguments.max_new_tokens,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(code + "\n", encoding="utf-8")
    print(code)


if __name__ == "__main__":
    main()
