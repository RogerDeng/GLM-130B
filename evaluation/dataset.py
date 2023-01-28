import os
import math
import json

import numpy as np
import torch

from typing import List, Union
from abc import ABC, abstractmethod
from scipy.linalg import block_diag
from itertools import accumulate
from bisect import bisect_right

from SwissArmyTransformer import get_tokenizer

from .configs import BaseConfig, MultiChoiceTaskConfig, GenerationTaskConfig, LanguageModelTaskConfig
from .utils import get_tokenized_input
from .model import ModelForEvaluation


def pad_batch(tokens, position_ids, attention_mask, max_seq_length):
    pad_length = max_seq_length - len(tokens)
    attention_mask = np.pad(
        attention_mask,
        pad_width=((0, pad_length),),
        mode="constant",
        constant_values=0,
    )
    tokens = np.concatenate((tokens, np.zeros(pad_length, dtype=np.int64)))
    position_ids = np.concatenate(
        (position_ids, np.zeros_like(position_ids[..., -1:], dtype=np.int64).repeat(pad_length, -1)), axis=-1
    )
    return tokens, position_ids, attention_mask


class EvaluationDataset(torch.utils.data.Dataset, ABC):
    """
    Jsonlines of {
        "text": context
        "choices": [choice_id1,...], if not None, len(target) == 1
        "label": If generation task -1, else [0, len(choices))
    }
    If [MASK] not in context, will append [MASK] after text
    """

    def __init__(self, path: Union[str, List[str]], model: ModelForEvaluation, config: BaseConfig):
        self.path = path if isinstance(path, list) else [path]
        self.model = model
        self.config = config
        self.max_seq_length = self.config.max_seq_length
        self.dtype = np.int64

        self.tokenizer = get_tokenizer()
        self.mask_id = self.tokenizer.get_command("[MASK]")
        self.gmask_id = self.tokenizer.get_command("[gMASK]")

        self.data = []
        for p in self.path:
            self.process_single_file(p)

    @property
    def has_collate_fn(self) -> bool:
        return False

    def collate_fn(self, samples):
        return None

    def process_single_file(self, path):
        with open(os.path.join(path), "r", encoding="utf-8") as file:
            for line in file:
                item = json.loads(line)
                self.data.extend(self.process_single_item(item))

    @abstractmethod
    def process_single_item(self, item, **kwargs) -> List[dict]:
        pass

    def __len__(self):
        return len(self.data)


class GenerationTaskDataset(EvaluationDataset):
    config: GenerationTaskConfig

    def process_single_item(self, item, **kwargs):
        text, targets = get_tokenized_input(item, "inputs"), get_tokenized_input(item, "targets")
        if len(targets) and (not isinstance(targets[0], list)):
            targets = [targets]
        if len(text) + self.config.max_gen_length + 2 > self.config.max_seq_length:
            text_length = self.config.max_seq_length - self.config.max_gen_length - 2
            text = text[len(text) - text_length : len(text)]
        return [{"text": text, "targets": targets, **kwargs}]

    @property
    def has_collate_fn(self) -> bool:
        return True

    def collate_fn(self, samples):
        TILE = 32
        length_to_pad = (max(map(lambda spl: len(spl["token"]), samples)) + TILE - 1) // TILE * TILE

        token_batch, position_id_batch, attention_mask_batch = [], [], []
        context_length_batch, target_position_id_batch = [], []

        for sample in samples:
            token, position_id, attention_mask = pad_batch(
                sample["token"], sample["position_id"], sample["attention_mask"], length_to_pad
            )
            token_batch.append(token)
            position_id_batch.append(position_id)
            attention_mask_batch.append(attention_mask)
            context_length_batch.append(sample["context_length"])
            target_position_id_batch.append(sample["target_position_id"])
        return {
            "tokens": torch.tensor(np.array(token_batch), dtype=torch.int64),
            "position_ids": torch.tensor(np.array(position_id_batch), dtype=torch.int64),
            "attention_mask": torch.tensor(np.array(attention_mask_batch), dtype=torch.int64) < 0.5,
            "context_length": torch.tensor(context_length_batch, dtype=torch.int64),
            "target_position_ids": torch.tensor(np.array(target_position_id_batch), dtype=torch.int64),
        }

    def __getitem__(self, idx):
        item = self.data[idx]
        sample = self.model.build_generation_sample(
            item["text"],
            max_gen_length=self.config.max_gen_length,
            use_task_mask=self.config.use_task_mask,
            unidirectional=self.config.unidirectional,
        )
        return sample


class MultiChoiceTaskDataset(EvaluationDataset):
    config: MultiChoiceTaskConfig

    def __init__(self, path: Union[str, List[str]], model: ModelForEvaluation, config: BaseConfig):
        self.is_single_token = True  # set to False later in process_single_item func
        super().__init__(path, model, config)

    @property
    def has_collate_fn(self) -> bool:
        return True

    def collate_fn(self, samples):
        TILE = 32
        length_to_pad = (max(map(lambda spl: len(spl["token"]), samples)) + TILE - 1) // TILE * TILE

        token_batch, position_id_batch, attention_mask_batch = [], [], []
        choices_batch, choice_target_ids_batch = [], []

        for sample in samples:
            token, position_id, attention_mask = pad_batch(
                sample["token"], sample["position_id"], sample["attention_mask"], length_to_pad
            )
            token_batch.append(token)
            position_id_batch.append(position_id)
            attention_mask_batch.append(attention_mask)
            choices_batch.append(sample["choices"])
            choice_target_ids_batch.append(sample["choice_target_ids"])

        return {
            "tokens": torch.tensor(np.array(token_batch), dtype=torch.int64),
            "position_ids": torch.tensor(np.array(position_id_batch), dtype=torch.int64),
            "attention_mask": torch.tensor(np.array(attention_mask_batch), dtype=torch.int64) < 0.5,
            "choices": choices_batch,
            "choice_target_ids": choice_target_ids_batch,
            "is_single_token": self.is_single_token,
        }

    def process_single_item(self, item, **kwargs):
        text, choices, label = get_tokenized_input(item, "inputs"), get_tokenized_input(item, "choices"), item["label"]

        tgt_seq_length = sum([len(choice) for choice in choices])
        if tgt_seq_length == len(choices):
            # For single token, we only insert one [sop]
            tgt_seq_length = 1

        assert tgt_seq_length < self.config.max_seq_length
        if len(text) + tgt_seq_length + 2 > self.config.max_seq_length:
            text_length = self.config.max_seq_length - tgt_seq_length - 2
            text = text[len(text) - text_length : len(text)]

        assert not (
            self.mask_id in text and self.config.use_multitask_encoding
        ), "Unified multitask encoding don't support blank filling"

        if tgt_seq_length != 1:
            self.is_single_token = False

        return [{"text": text, "choices": choices, "label": label, **kwargs}]

    def __getitem__(self, idx):
        item = self.data[idx]
        sample = self.model.build_multiple_choice_sample(
            item["text"],
            item["choices"],
            is_single_token=self.is_single_token,
            unified_multitask_encoding=self.config.use_multitask_encoding,
            unidirectional=self.config.unidirectional,
            use_task_mask=self.config.use_task_mask,
        )
        return sample


class LanguageModelTaskDataset(EvaluationDataset):
    config: LanguageModelTaskConfig
    left_weights: List[int]
    weights: List[int]

    def process_single_file(self, path):
        num_sequences = []
        with open(os.path.join(path), "r", encoding="utf-8") as file:
            raw_text = file.read()
            tokens = self.tokenizer.tokenize(raw_text)
            self.data.append(
                {
                    "raw_text": tokens,
                    "num_original_tokens": len(raw_text.strip().split(" ")),
                    "num_sequences": max(
                        math.ceil(
                            max(len(tokens) - (self.config.max_seq_length - 1), 0) / self.config.generation_length
                        )
                        + 1,
                        1,
                    ),
                }
            )
            num_sequences.append(self.data[-1]["num_sequences"])
        self.weights = list(accumulate(num_sequences))
        self.left_weights = [0] + self.weights[:-1]

    def process_single_item(self, item):
        pass

    def __len__(self):
        return self.data[0]["num_sequences"]

    def __getitem__(self, idx):
        document_idx = bisect_right(self.weights, idx)
        idx = idx - self.left_weights[document_idx]
        start_idx = idx * self.config.generation_length
        end_idx = start_idx + self.config.max_seq_length - 1  # for additional [gMASK]
        tokens = self.data[document_idx]["raw_text"][start_idx:end_idx]

        return self.model.build_language_model_sample(
            tokens,
            is_first_segment=idx == 0,
            max_seq_length=self.config.max_seq_length,
            generation_length=self.config.generation_length,
            unidirectional=self.config.unidirectional,
            use_gmask=self.config.use_task_mask,
        )
