# encoding: utf-8
"""
@author: Meefly
@contact: admin@meijiex.vip

@version: 1.0
@file: vSnet.py
@time: 2019年1月7日 17:36:57

这一行开始写关于本文件的说明与解释
"""
import pdb
import logging
import numpy as np
from typing import Any, Dict, List, Optional, Tuple

import torch
from torch.nn.functional import nll_loss

from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Highway
from allennlp.modules import Seq2SeqEncoder, TimeDistributed, TextFieldEmbedder
from allennlp.modules.matrix_attention.dot_product_matrix_attention import DotProductMatrixAttention
from allennlp.modules.matrix_attention.matrix_attention import MatrixAttention
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import BooleanAccuracy, CategoricalAccuracy
from allennlp.training.metrics.bleu import BLEU

from .MsmarcoRouge import MsmarcoRouge

logger = logging.getLogger(__name__)


@Model.register('vSnet')
class vSnet(Model):
    """
    This class implements Yizhong Wang's Multi-Passage Machine Reading Comprehension with Cross-Passage
    Answer Verification (https://arxiv.org/abs/1805.02220)
    The basic layout is pretty simple: encode words as a combination of word embeddings and a
    character-level encoder, pass the word representations through a bi-LSTM/GRU, use a matrix of
    attentions to put question information into the passage word representations (this is the only
    part that is at all non-standard), pass this through another few layers of bi-LSTMs/GRUs, and
    do a softmax over span start and span end.

    Parameters
    ----------
    vocab : ``Vocabulary``
    text_field_embedder : ``TextFieldEmbedder``
        Used to embed the ``question`` and ``passage`` ``TextFields`` we get as input to the model.
    num_highway_layers : ``int``
        The number of highway layers to use in between embedding the input and passing it through
        the phrase layer.
    phrase_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between embedding tokens
        and doing the bidirectional attention.
    modeling_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between the bidirectional
        attention and predicting span start and end.
    dropout : ``float``, optional (default=0.2)
        If greater than 0, we will apply dropout with this probability after all encoders (pytorch
        LSTMs do not apply dropout to their last layer).
    mask_lstms : ``bool``, optional (default=True)
        If ``False``, we will skip passing the mask to the LSTM layers.  This gives a ~2x speedup,
        with only a slight performance decrease, if any.  We haven't experimented much with this
        yet, but have confirmed that we still get very similar performance with much faster
        training times.  We still use the mask for all softmaxes, but avoid the shuffling that's
        required when using masking with pytorch LSTMs.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """

    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 match_layer: Seq2SeqEncoder,
                 matrix_attention_layer: MatrixAttention,
                 modeling_layer: Seq2SeqEncoder,
                 span_end_lstm: Seq2SeqEncoder,
                 ptr_dim: int = 200,
                 dropout: float = 0.2,
                 num_passages: int = 10,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super().__init__(vocab, regularizer)
        self._vocab = vocab
        self.ptr_dim = ptr_dim
        self._text_field_embedder = text_field_embedder
        self._highway_layer = TimeDistributed(Highway(text_field_embedder.get_output_dim(),
                                                      num_highway_layers))
        self._phrase_layer = phrase_layer
        self._matrix_attention = DotProductMatrixAttention()
        self._modeling_layer = modeling_layer
        modeling_dim = modeling_layer.get_output_dim()
        encoding_dim = phrase_layer.get_output_dim()

        self._match_layer = match_layer
        self._ptr_layer_1 = TimeDistributed(torch.nn.Linear(encoding_dim * 4 +
                                                            modeling_dim +
                                                            ptr_dim, ptr_dim))
        self._ptr_layer_2 = TimeDistributed(torch.nn.Linear(ptr_dim, 1))

        self._content_layer_1 = TimeDistributed(torch.nn.Linear(encoding_dim * 4 +
                                                                modeling_dim, ptr_dim))
        self._content_layer_2 = TimeDistributed(torch.nn.Linear(ptr_dim, 1))

        self._passages_matrix_attention = matrix_attention_layer
        self._span_end_lstm = span_end_lstm
        self._span_end_lstm._module.weight_hh_l0.requires_grad = False
        self._span_end_lstm._module.weight_hh_l1.requires_grad = False

        self._passage_predictor = TimeDistributed(torch.nn.Linear(num_passages, 1))

        self._start_h_embedding = torch.nn.Parameter(data=torch.zeros(1, 1, 1).float(),
                                                     requires_grad=False)

        self._span_start_accuracy = CategoricalAccuracy()
        self._span_end_accuracy = CategoricalAccuracy()
        self._span_accuracy = BooleanAccuracy()
        self._rouge_metrics = MsmarcoRouge()
        self._bleu_metrics = BLEU()
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)

    def forward(self,  # type: ignore
                question: Dict[str, torch.LongTensor],
                passages: List[Dict[str, torch.LongTensor]],
                spans_start: List[List[torch.IntTensor]] = None,
                spans_end: List[List[torch.IntTensor]] = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passages : List[Dict[str, torch.LongTensor]]
            From a ``ListField[TextField]``.  The model assumes that one question corresponds to
            more than one article, and each article can correspond to more than one answer. And it
            will predict the beginning and ending positions of the answer within the passage.
        spans_start : ``List[List[torch.IntTensor]]``, optional
            From an ``ListField[ListField[IndexField]]``.  This is one of the things we are trying
            to predict - the beginning position of the answer with the passage.  This is an
            `inclusive` token index. If this is given, we will compute a loss that gets included in
            the output dictionary.
        spans_end : ``List[List[torch.IntTensor]]``, optional
            From an ``ListField[ListField[IndexField]]``.  This is one of the things we are trying
            to predict - the ending position of the answer with the passage.  This is an `inclusive`
            token index. If this is given, we will compute a loss that gets included in the output
            dictionary.
        metadata : ``List[Dict[str, Any]]``, optional
            If present, this should contain the question ID, original passage text, and token
            offsets into the passage for each instance in the batch.  We use this for computing
            official metrics using the official MSMARCO bleu-1 and rouge-L evaluation script.
            The length of this list should be the batch size, and each dictionary should have the
            keys ``qid``, ``original_passages``, ``question_tokens`` , ``passage_tokens``, and
            ``passages_offsets``.
        Returns
        -------
        An output dictionary consisting of:
        spans_start_logits : List[torch.FloatTensor]
            A tensor of shape ``(batch_size, num_passages, passage_length)`` representing unnormalized
            log probabilities of the span start position.
        spans_start_probs : List[torch.FloatTensor]
            The result of ``softmax(spans_start_logits)``.
        spans_end_logits : List[torch.FloatTensor]
            A tensor of shape ``(batch_size, num_passages, passage_length)`` representing unnormalized
            log probabilities of the span end position (inclusive).
        spans_end_probs : List[torch.FloatTensor]
            The result of ``softmax(span_end_logits)``.
        best_passage_id: torch.IntTensor
            The idx of the best answer source article.        in range(0,num_passages)
        best_span : List[torch.IntTensor]
            The result of a constrained inference over ``span_start_logits`` and
            ``span_end_logits`` to find the most probable span.  Shape is ``(batch_size, num_passages, 2)``
            and each offset is a token index.
        loss : torch.FloatTensor, optional
            A scalar loss to be optimised.
        best_span_str : List[str]
            If sufficient metadata was provided for the instances in the batch, we also return the
            string from the original passage that the model thinks is the best answer to the
            question.
        """
        # ---------------------------------------
        # Part One: Question and Passage Modeling
        # ---------------------------------------
        # passages['token_characters']
        #   torch.Size([batch_size, num_passage, passage_length, num_characters])
        # passages['tokens']
        #   torch.Size([batch_size, num_passage, passage_length])
        # shape(passages_batch_size=num_passage*batch_size, question_length, question_embedding_size )
        batch_size, num_passages, passage_length, num_characters = passages['token_characters'].size()
        # shape(batch_size*num_passage, passage_length, num_characters)
        batch_passages = {}
        batch_passages['token_characters'] = passages['token_characters'].view(-1,
                                                                               passage_length,
                                                                               num_characters)
        # shape(batch_size*num_passage, passage_length)
        batch_passages['tokens'] = passages['tokens'].view(-1, passage_length)
        # shape(batch_size*num_passage, passage_length, embedding_dim)
        # print(batch_passages)
        try:
            embedded_passages = self._highway_layer(self._text_field_embedder(batch_passages))
        except Exception as e:
            pdb.set_trace()
            raise e
        embedding_dim = embedded_passages.size(-1)

        # shape(batch_size, question_length, num_characters)
        questions = {}
        batch_size, question_length, num_characters = question['token_characters'].size()
        questions['token_characters'] = question['token_characters'].repeat(1, num_passages, 1)\
                                                                    .view(-1,
                                                                          question_length,
                                                                          num_characters)
        questions['tokens'] = question['tokens'].repeat(1, num_passages).view(-1, question_length)
        embedded_question = self._highway_layer(self._text_field_embedder(question))
        embedding_size = embedded_question.size(-1)
        # shape(num_passages*batch_size, question_length, embedding_size)
        embedded_questions = embedded_question.repeat(1, num_passages, 1)\
                                              .view(-1, question_length, embedding_size)
        assert embedded_questions.size(0) == embedded_passages.size(0)
        assert torch.eq(embedded_questions[0, :, :], embedded_questions[num_passages - 1, :, :]).all()

        # shape(num_passages*batch_size, question_length)
        questions_mask = util.get_text_field_mask(questions).float()
        # shape(num_passages*batch_size, passage_length)
        passages_mask = util.get_text_field_mask(batch_passages).float()

        # shape(num_passages*batch_size, question_length)
        questions_lstm_mask = questions_mask if self._mask_lstms else None
        # shape(num_passages*batch_size, passage_length)
        passages_lstm_mask = passages_mask if self._mask_lstms else None

        # encoded_question
        #     torch.Size([num_passages*batch_size, question_length, phrase_layer_encoding_dim])
        encoded_questions = self._dropout(self._phrase_layer(embedded_questions, questions_lstm_mask))
        phrase_layer_encoding_dim = encoded_questions.size(-1)
        # encoded_passages
        #     torch.Size([num_passages*batch_size, passage_length, phrase_layer_encoding_dim])
        encoded_passages = self._dropout(self._phrase_layer(embedded_passages, passages_lstm_mask))
        # Shape: (num_passages*batch_size, passage_length, question_length)
        passages_questions_similarity = self._matrix_attention(encoded_passages, encoded_questions)
        # Shape: (num_passages*batch_size, passage_length, question_length)
        passages_questions_attention = util.masked_softmax(passages_questions_similarity, questions_mask)
        # Shape: (num_passages*batch_size, passage_length, phrase_layer_encoding_dim)
        passages_questions_vectors = util.weighted_sum(encoded_questions, passages_questions_attention)

        # We replace masked values with something really negative here, so they don't affect the
        # max below.
        masked_similarity = util.replace_masked_values(passages_questions_similarity,
                                                       questions_mask.unsqueeze(1),
                                                       -1e7)
        # Shape: (batch_size * num_passages, passage_length)
        questions_passages_similarity = masked_similarity.max(dim=-1)[0].squeeze(-1)
        # Shape: (batch_size * num_passages, passage_length)
        questions_passages_attention = util.masked_softmax(questions_passages_similarity, passages_mask)
        # Shape: (batch_size * num_passages, phrase_layer_encoding_dim)
        questions_passages_vector = util.weighted_sum(encoded_passages, questions_passages_attention)
        # Shape: (batch_size * num_passages, passage_length, phrase_layer_encoding_dim)
        tiled_questions_passages_vector = questions_passages_vector.unsqueeze(1)\
                                                                   .expand(batch_size * num_passages,
                                                                           passage_length,
                                                                           phrase_layer_encoding_dim)

        # Shape: (batch_size, passage_length, phrase_layer_encoding_dim * 4)
        final_merged_passage = torch.cat([encoded_passages,
                                          passages_questions_vectors,
                                          encoded_passages * passages_questions_vectors,
                                          encoded_passages * tiled_questions_passages_vector],
                                         dim=-1)

        modeled_passage = self._dropout(self._modeling_layer(final_merged_passage, passages_lstm_mask))
        # modeling_dim = modeled_passage.size(-1)

        # ------------------------------------
        # Part Two: Answer Boundary Prediction
        # ------------------------------------
        # # Shape: (num_passages*batch_size, passage_length, phrase_layer_encoding_dim)
        # match_passages_vector = self._dropout(self._match_layer(passages_questions_vectors,
        #                                                         passages_lstm_mask))
        # Shape: (batch_size * num_passages, passage_length, encoding_dim * 4 + modeling_dim))
        match_passages_vector = self._dropout(torch.cat([final_merged_passage, modeled_passage], dim=-1))

        # Shape: (num_passages*batch_size, passage_length, ptr_dim)
        # Shape: (num_passages*batch_size, passage_length)
        span_start_logits = self._ptr_layer_2(torch.tanh(self._ptr_layer_1(
                                              torch.cat((match_passages_vector,
                                                         self._start_h_embedding.repeat(num_passages *
                                                                                        batch_size,
                                                                                        passage_length,
                                                                                        self.ptr_dim)),
                                                        dim=-1)))).squeeze(-1)
        # shape(num_passages*batch_size, passage_length)
        span_start_probs = util.masked_softmax(span_start_logits, passages_mask)
        # shape(num_passages*batch_size, 1, encoding_dim * 4 + modeling_dim)
        c = torch.matmul(match_passages_vector.transpose(1, 2),
                         span_start_probs.unsqueeze(2)).squeeze(-1).unsqueeze(1)
        # shape(num_passages*batch_size, 1, ptr_dim)
        end_h_embedding = self._span_end_lstm(c, torch.ones(c.size()[:2]).to(c.device))
        span_end_logits = self._ptr_layer_2(torch.tanh(self._ptr_layer_1(
                                            torch.cat((match_passages_vector,
                                                       end_h_embedding.repeat(1,
                                                                              passage_length,
                                                                              1)),
                                                      dim=-1)))).squeeze(-1)
        # shape(num_passages*batch_size, passage_length)
        span_end_probs = util.masked_softmax(span_end_logits, passages_mask)

        # -----------------------------------
        # Part Three: Answer Content Modeling
        # -----------------------------------
        relu = torch.nn.ReLU()
        # shape(num_passages*batch_size, passage_length)
        p = torch.sigmoid(self._content_layer_2(relu(self._content_layer_1(
            match_passages_vector)))).squeeze(-1)

        # embedded_passages shape(batch_size*num_passages, passage_length, embedding_dim)
        # get answers candidates
        # shape(num_passages*batch_size, passage_length)
        ground_truth_p = self.map_span_to_01(spans_end, p.size()) -\
            self.map_span_to_01(spans_start - 1, p.size())
        embedded_answers_candidates = embedded_passages *\
            ground_truth_p.view(batch_size * num_passages, passage_length, -1).repeat(1, 1, embedding_dim)
        # shape(num_passages*batch_size, embedding_dim)

        r = util.weighted_sum(embedded_answers_candidates, p)

        # ---------------------------------------------
        # Part Four:  Cross-Passage Answer Verification
        # ---------------------------------------------
        # shape(batch_size, num_passages, embedding_dim)
        batch_r = r.view(batch_size, num_passages, -1)
        set_diagonal_zero = (1 - torch.eye(num_passages)).unsqueeze(0).repeat(batch_size, 1, 1)
        set_diagonal_zero = set_diagonal_zero.to(c.device)
        # shape(batch_size, num_passages, num_passages)
        passages_self_similarity = self._matrix_attention(batch_r, batch_r) * set_diagonal_zero
        # shape(batch_size, num_passages, num_passages)
        passages_self_attention = torch.softmax(passages_self_similarity, dim=-1)
        # shape(batch_size, num_passages, embedding_dim)
        attention_batch_r = util.weighted_sum(batch_r, passages_self_attention)

        # shape(batch_size, num_passages, num_passages)
        g = self._passages_matrix_attention(batch_r, attention_batch_r)
        # shape(batch_size, num_passages)
        passages_verify = self._passage_predictor(g).squeeze(-1)
        best_span = self.get_best_span(span_start_probs.view(batch_size, num_passages, -1),
                                       span_end_probs.view(batch_size, num_passages, -1),
                                       util.masked_softmax(p, passages_mask).view(batch_size,
                                                                                  num_passages,
                                                                                  -1),
                                       torch.softmax(passages_verify, dim=-1))
        best_passage_id = best_span[:, 0]
        # best_passage_id = spans_start[(spans_start != -1)]
        # reshape_best_passage_id = (torch.arange(0, batch_size) * num_passages).unsqueeze(-1) +\
        #     best_passage_id.unsqueeze(-1).cpu()

        output_dict = {'best_span': best_span,
                       'span_start_logits': span_start_logits.view(batch_size, num_passages, -1),
                       'span_start_probs': span_start_probs.view(batch_size, num_passages, -1),
                       'span_end_logits': span_end_logits.view(batch_size, num_passages, -1),
                       'span_end_probs': span_end_probs.view(batch_size, num_passages, -1),
                       'best_passage_id': best_passage_id}

        if metadata is not None:
            output_dict['best_span_str'] = []
            question_tokens = []
            passage_tokens = []
            for i in range(batch_size):
                question_tokens.append(metadata[i]['question_tokens'])
                passage_tokens.append(metadata[i]['passage_tokens'])
                passage_str = metadata[i]['original_passages']
                offsets = metadata[i]['passages_offsets']
                passage_id, start_idx, end_idx = tuple(best_span[i, :].detach().cpu().numpy())
                # passage_id = max(0, min(passage_id, len(offsets) - 1))
                # clamp start_idx and end_idx to range(0, passage_length - 1)
                start_idx = max(0, min(start_idx, len(offsets[passage_id]) - 1))
                end_idx = max(0, min(end_idx, len(offsets[passage_id]) - 1))

                start_offset = offsets[passage_id][start_idx][0]
                end_offset = offsets[passage_id][end_idx][1]
                best_span_string = passage_str[passage_id][start_offset:end_offset]
                output_dict['best_span_str'].append(best_span_string)
                answer_texts = metadata[i].get('answer_texts', [])
                answer_texts = list(set([item for sublist in answer_texts for item in sublist]))
                # answer_texts = answer_texts[np.array([len(text) for text in answer_texts]).argmax()]
                # answer_texts = answer_texts[passage_id] or ['']
                if answer_texts:
                    self._rouge_metrics(best_span_string, answer_texts)
                    # self._span_start_accuracy(span_start_probs[reshape_best_passage_id].squeeze(),
                    #                           spans_start[reshape_best_passage_id].squeeze(),
                    #                           (spans_start[reshape_best_passage_id].squeeze() != -1))
                    # self._span_end_accuracy(span_end_probs[reshape_best_passage_id].squeeze(),
                    #                         spans_end[reshape_best_passage_id].squeeze(),
                    #                         (spans_end[reshape_best_passage_id].squeeze() != -1))
                    self._span_start_accuracy(span_start_probs.view(batch_size, num_passages, -1),
                                              spans_start.view(batch_size, num_passages),
                                              (spans_start.view(batch_size, num_passages) != -1))
                    self._span_end_accuracy(span_end_probs.view(batch_size, num_passages, -1),
                                            spans_end.view(batch_size, num_passages),
                                            (spans_end.view(batch_size, num_passages) != -1))
                    # self._bleu_metrics(best_span_string, answer_text)
                # if loss < 9:
                #     print()
                #     print(output_dict['best_span_str'][i])
                #     print(answer_texts)
            output_dict['question_tokens'] = question_tokens
            output_dict['passage_tokens'] = passage_tokens

        if spans_start is not None:
            # span_start_probs shape(num_passages*batch_size, passage_length)
            # spans_start shape(batch_size, num_passages, 1)
            # then shape(batch_size*num_passages, 1)
            spans_start = spans_start.squeeze(-1).view(batch_size * num_passages, -1)
            spans_end = spans_end.squeeze(-1).view(batch_size * num_passages, -1)

            spans_start.clamp_(-1, passage_length - 1)
            spans_end.clamp_(-1, passage_length - 1)
            # loss_Boundary = nll_loss(util.masked_log_softmax(
            #     span_start_logits[reshape_best_passage_id].squeeze(),
            #     passages_mask[reshape_best_passage_id].squeeze()),
            #     spans_start[reshape_best_passage_id].squeeze(), ignore_index=-1)
            # loss_Boundary += nll_loss(util.masked_log_softmax(
            #     span_end_logits[reshape_best_passage_id].squeeze(),
            #     passages_mask[reshape_best_passage_id].squeeze()),
            #     spans_end[reshape_best_passage_id].squeeze(), ignore_index=-1)
            loss_Boundary = nll_loss(util.masked_log_softmax(span_start_logits, passages_mask),
                                     spans_start.squeeze(-1), ignore_index=-1)
            loss_Boundary += nll_loss(util.masked_log_softmax(span_end_logits, passages_mask),
                                      spans_end.squeeze(-1), ignore_index=-1)
            loss_Boundary = loss_Boundary / 2

            loss_Content = -torch.mean(util.masked_log_softmax(p, passages_mask) *
                                       ground_truth_p)

            # shape(batch_size, num_passages)
            ground_truth_passages_verify = (spans_end != -1).float().to(c.device).view(batch_size,
                                                                                       num_passages)
            loss_Verification = torch.log_softmax(passages_verify, dim=-1) * ground_truth_passages_verify
            loss_Verification = -torch.mean(loss_Verification)
            loss = loss_Boundary + 0.5 * loss_Content + 0.5 * loss_Verification
            # print('\nloss_Boundary\t\t', loss_Boundary)
            # print('loss_Content\t\t', loss_Content)
            # print('loss_Verification\t', loss_Verification)
            output_dict['loss'] = loss

        output_dict['qids'] = [data['qid'] for data in metadata]
        return output_dict


    @staticmethod
    def map_span_to_01(span_idx: torch.Tensor, shape: Tuple) -> torch.Tensor:
        '''
        Parameters
        ----------
        span_idx shape(batch_size * num_passages, 1)
        '''
        device = span_idx.device
        x = (torch.arange(0, shape[-1]).view(1, -1).expand(shape[0], -1).float().to(device) -
             span_idx.float().view(-1, 1).expand(-1, shape[-1]))
        return torch.where(x > 0, torch.zeros(shape).to(device), torch.ones(shape).to(device))

    @staticmethod
    def get_best_span(span_start_probs: torch.Tensor,
                      span_end_probs: torch.Tensor,
                      content: torch.Tensor,
                      passages_verify: torch.Tensor) -> torch.Tensor:
        '''
        Parameters
        ----------
        span_start_probs: shape(batch_size, num_passages, passage_length)
        span_end_probs: shape(batch_size, num_passages, passage_length)
        content: shape(batch_size, num_passages, passage_length)
        passages_verify: shape(batch_size, num_passages)

        Return
        ------
        best_word_span: shape(batch_size, 3)
            3 for [best_passage_id, start, end]
        '''
        if span_start_probs.dim() != 3 or span_end_probs.dim() != 3:
            raise ValueError("Input shapes must be (batch_size, num_passages, passage_length)")
        batch_size, num_passages, passage_length = span_start_probs.size()
        max_span_prob = np.ones((batch_size, num_passages)) * -1e20
        max_span_batch = np.ones((batch_size)) * -1e20
        span_start_argmax = torch.zeros(batch_size, num_passages).long()
        best_word_span = span_start_probs.new_zeros((batch_size, 3), dtype=torch.long)

        span_start_probs_clone = span_start_probs.clone().detach().cpu().numpy()
        span_end_probs_clone = span_end_probs.clone().detach().cpu().numpy()
        content_clone = content.clone().detach().cpu().numpy()
        passages_verify_clone = passages_verify.clone().detach().cpu().numpy()
        for b in range(batch_size):
            for p in range(num_passages):
                for j in range(passage_length):
                    val1 = span_start_probs_clone[b, p, span_start_argmax[b, p]]
                    if val1 < span_start_probs_clone[b, p, j]:
                        span_start_argmax[b, p] = j
                        val1 = span_start_probs_clone[b, p, j]

                    val2 = span_end_probs_clone[b, p, j]
                    # pdb.set_trace()
                    value = (val1 * val2) *\
                        np.mean(content_clone[b, p, span_start_argmax[b, p]:j + 1]) *\
                        passages_verify_clone[b, p]
                    # value = val1 + val2

                    if value > max_span_prob[b, p]:
                        max_span_prob[b, p] = value
                        # print(span_end_probs_clone[b, p, j:])
                        if max_span_prob[b, p] > max_span_batch[b]:
                            best_word_span[b, 0] = p
                            best_word_span[b, 1] = span_start_argmax[b, p]
                            best_word_span[b, 2] = j
                            max_span_batch[b] = max_span_prob[b, p]
        return best_word_span

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        rouge_l = self._rouge_metrics.get_metric(reset)
        # bleu_1 = self._bleu_metrics.get_metric(reset)
        return {'start_acc': self._span_start_accuracy.get_metric(reset),
                'end_acc': self._span_end_accuracy.get_metric(reset),
                # 'span_acc': self._span_accuracy.get_metric(reset),
                'rouge_L': rouge_l}
        # 'bleu_1': bleu_1}