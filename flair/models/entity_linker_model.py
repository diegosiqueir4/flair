import logging
import re
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Set, Union
from unicodedata import category

import torch

import flair.embeddings
import flair.nn
from flair.data import Dictionary, Sentence, Span
from flair.file_utils import cached_path

log = logging.getLogger("flair")


class CandidateGenerator:
    """
    Given a string, the CandidateGenerator returns possible target classes as candidates.
    """

    def __init__(self, candidates: Union[str, Dict], backoff: bool = True):
        # internal candidate lists of generator
        self.mention_to_candidates_map: Dict = {}

        # load Zelda candidates if so passed
        if isinstance(candidates, str) and candidates.lower() == "zelda":
            zelda_path: str = "https://flair.informatik.hu-berlin.de/resources/datasets/zelda"
            zelda_candidates = cached_path(f"{zelda_path}/zelda_mention_entities_counter.pickle", cache_dir="datasets")
            import pickle

            with open(zelda_candidates, "rb") as handle:
                mention_entities_counter = pickle.load(handle)

            # create candidate lists
            candidate_lists = {}
            for mention in mention_entities_counter:
                candidate_lists[mention] = list(mention_entities_counter[mention].keys())

            self.mention_to_candidates_map = candidate_lists

        elif isinstance(candidates, Dict):
            self.mention_to_candidates_map = candidates

        # if lower casing is enabled, create candidate lists of lower cased versions
        self.backoff = backoff
        if self.backoff:
            # create a new dictionary for lower cased mentions
            lowercased_mention_to_candidates_map: Dict = {}

            # go through each mention and its candidates
            for mention, candidates in self.mention_to_candidates_map.items():
                backoff_mention = self._make_backoff_string(mention)
                # check if backoff mention already seen. If so, add candidates. Else, create new entry.
                if backoff_mention in lowercased_mention_to_candidates_map:
                    current_candidates = lowercased_mention_to_candidates_map[backoff_mention]
                    lowercased_mention_to_candidates_map[backoff_mention] = set(current_candidates).union(candidates)
                else:
                    lowercased_mention_to_candidates_map[backoff_mention] = candidates

            # set lowercased version as map
            self.mention_to_candidates_map = lowercased_mention_to_candidates_map

    @lru_cache(maxsize=50000)
    def _make_backoff_string(self, mention: str) -> str:
        backoff_mention = mention.lower()
        backoff_mention = "".join(ch for ch in backoff_mention if category(ch)[0] not in "P")
        backoff_mention = re.sub(" +", " ", backoff_mention)
        return backoff_mention

    def get_candidates(self, mention: str) -> Set[str]:
        """Given a mention, this method returns a set of candidate classes"""
        if self.backoff:
            mention = self._make_backoff_string(mention)

        return set(self.mention_to_candidates_map[mention]) if mention in self.mention_to_candidates_map else set()


class EntityLinker(flair.nn.DefaultClassifier[Sentence, Span]):
    """
    Entity Linking Model
    The model expects text/sentences with annotated entity mentions and predicts entities to these mentions.
    To this end a word embedding is used to embed the sentences and the embedding of the entity mention goes through a linear layer to get the actual class label.
    The model is able to predict '<unk>' for entity mentions that the model can not confidently match to any of the known labels.
    """

    def __init__(
        self,
        embeddings: flair.embeddings.TokenEmbeddings,
        label_dictionary: Dictionary,
        pooling_operation: str = "first_last",
        label_type: str = "nel",
        candidates: Optional[CandidateGenerator] = None,
        **classifierargs,
    ):
        """
        Initializes an EntityLinker
        :param embeddings: embeddings used to embed the words/sentences
        :param label_dictionary: dictionary that gives ids to all classes. Should contain <unk>
        :param pooling_operation: either 'average', 'first', 'last' or 'first&last'. Specifies the way of how text representations of entity mentions (with more than one word) are handled.
        E.g. 'average' means that as text representation we take the average of the embeddings of the words in the mention. 'first&last' concatenates
        the embedding of the first and the embedding of the last word.
        :param label_type: name of the label you use.
        """

        super(EntityLinker, self).__init__(
            embeddings=embeddings,
            label_dictionary=label_dictionary,
            final_embedding_size=embeddings.embedding_length * 2
            if pooling_operation == "first_last"
            else embeddings.embedding_length,
            **classifierargs,
        )

        self.pooling_operation = pooling_operation
        self._label_type = label_type

        cases: Dict[str, Callable[[Span, List[str]], torch.Tensor]] = {
            "average": self.emb_mean,
            "first": self.emb_first,
            "last": self.emb_last,
            "first_last": self.emb_firstAndLast,
        }

        if pooling_operation not in cases:
            raise KeyError('pooling_operation has to be one of "average", "first", "last" or "first_last"')

        self.aggregated_embedding = cases[pooling_operation]

        self.candidates = candidates

        self.to(flair.device)

    def emb_first(self, span: Span, embedding_names):
        return span.tokens[0].get_embedding(embedding_names)

    def emb_last(self, span: Span, embedding_names):
        return span.tokens[-1].get_embedding(embedding_names)

    def emb_firstAndLast(self, span: Span, embedding_names):
        return torch.cat(
            (span.tokens[0].get_embedding(embedding_names), span.tokens[-1].get_embedding(embedding_names)), 0
        )

    def emb_mean(self, span, embedding_names):
        return torch.mean(torch.cat([token.get_embedding(embedding_names) for token in span], 0), 0)

    def _get_data_points_from_sentence(self, sentence: Sentence) -> List[Span]:
        return sentence.get_spans(self.label_type)

    def _filter_data_point(self, data_point: Sentence) -> bool:
        return bool(data_point.get_labels(self.label_type))

    def _get_embedding_for_data_point(self, prediction_data_point: Span) -> torch.Tensor:
        return self.aggregated_embedding(prediction_data_point, self.embeddings.get_names())

    def _get_state_dict(self):
        model_state = {
            **super()._get_state_dict(),
            "word_embeddings": self.embeddings.save_embeddings(use_state_dict=False),
            "label_type": self.label_type,
            "label_dictionary": self.label_dictionary,
            "pooling_operation": self.pooling_operation,
            "loss_weights": self.weight_dict,
            "candidates": self.candidates,
        }
        return model_state

    def _print_predictions(self, batch, gold_label_type):
        lines = []
        for datapoint in batch:
            eval_line = f"\n{datapoint.to_original_text()}\n"

            for span in datapoint.get_spans(gold_label_type):
                symbol = "✓" if span.get_label(gold_label_type).value == span.get_label("predicted").value else "❌"
                eval_line += (
                    f' - "{span.text}" / {span.get_label(gold_label_type).value}'
                    f' --> {span.get_label("predicted").value} ({symbol})\n'
                )

            lines.append(eval_line)
        return lines

    @classmethod
    def _init_model_with_state_dict(cls, state, **kwargs):
        # remap state dict for models serialized with Flair <= 0.11.3
        import re

        state_dict = state["state_dict"]
        for key in list(state_dict.keys()):
            state_dict[re.sub("^word_embeddings\\.", "embeddings.", key)] = state_dict.pop(key)

        return super()._init_model_with_state_dict(
            state,
            embeddings=state.get("word_embeddings"),
            label_dictionary=state.get("label_dictionary"),
            label_type=state.get("label_type"),
            pooling_operation=state.get("pooling_operation"),
            loss_weights=state.get("loss_weights", {"<unk>": 0.3}),
            candidates=state.get("candidates", None),
            **kwargs,
        )

    @property
    def label_type(self):
        return self._label_type

    def _mask_scores(self, scores: torch.Tensor, data_points: List[Span]):
        if not self.candidates:
            return scores

        masked_scores = -torch.inf * torch.ones(scores.size(), requires_grad=True, device=flair.device)

        for idx, span in enumerate(data_points):
            # get the candidates
            candidate_set = self.candidates.get_candidates(span.text)
            # during training, add the gold value as candidate
            if self.training:
                candidate_set.add(span.get_label(self.label_type).value)
            candidate_set.add("<unk>")
            indices_of_candidates = [self.label_dictionary.get_idx_for_item(candidate) for candidate in candidate_set]
            masked_scores[idx, indices_of_candidates] = scores[idx, indices_of_candidates]

        return masked_scores
