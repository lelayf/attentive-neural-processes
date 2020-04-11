import os
import numpy as np
import pandas as pd
import torch
import optuna
from tqdm.auto import tqdm
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader

from neural_processes.lightning import PL_Seq2Seq
from ..logger import logger
from ..utils import hparams_power


class NetTransformer(nn.Module):
    def __init__(self, hparams):
        super().__init__()
        hparams = hparams_power(hparams)
        self.hparams = hparams

        hidden_out_size = self.hparams.hidden_out_size
        enc_x_dim = self.hparams.x_dim + self.hparams.y_dim
        # self.enc_norm = BatchNormSequence(enc_x_dim)
        self.enc_emb = nn.Linear(enc_x_dim, hidden_out_size)
        encoder_norm = nn.LayerNorm(hidden_out_size)
        layer_enc = nn.TransformerEncoderLayer(
            d_model=hidden_out_size,
            dim_feedforward=self.hparams.hidden_size,
            dropout=self.hparams.attention_dropout,
            nhead=self.hparams.nhead,
            # activation
        )
        self.encoder = nn.TransformerEncoder(
            layer_enc, num_layers=self.hparams.nlayers, norm=encoder_norm
        )

        # self.dec_norm = BatchNormSequence(self.hparams.x_dim)
        # self.dec_emb = nn.Linear(self.hparams.x_dim, hidden_out_size)
        # layer_dec = nn.TransformerDecoderLayer(
        #     d_model=hidden_out_size,
        #     dim_feedforward=self.hparams.hidden_size,
        #     dropout=self.hparams.attention_dropout,
        #     nhead=self.hparams.nhead,
        # )
        # decoder_norm = nn.LayerNorm(hidden_out_size)
        # self.decoder = nn.TransformerDecoder(
        #     layer_dec,
        #     num_layers=self.hparams.nlayers,
        #     norm=decoder_norm
        # )
        self.mean = nn.Linear(hidden_out_size, self.hparams.y_dim)

    def forward(self, context_x, context_y, target_x, target_y=None):
        device = next(self.parameters()).device
        target_y_fake = (
            torch.ones(context_y.shape).float().to(device) * self.hparams.nan_value
        )
        context = torch.cat([context_x, context_y], -1).detach()
        target = torch.cat([target_x, target_y_fake], -1).detach()
        x = torch.cat([context, target * 1], 1).detach()

        # Masks
        x_mask = torch.isfinite(x) & (x != self.hparams.nan_value)
        x[~x_mask] = 0
        x = x.detach()
        x_key_padding_mask = ~x_mask.any(-1)
        # print('x_key_padding_mask', x_mask.float().mean())
        # print(x.shape, 'x1')
        x = self.enc_emb(x).permute(1, 0, 2)
        # print(x.shape, 'x2')
        # Size([C, B, emb_dim])
        outputs = self.encoder(x, src_key_padding_mask=x_key_padding_mask).permute(
            1, 0, 2
        )
        # print(outputs.shape, 'outputs')

        # Seems to help a little, especially with extrapolating out of bounds
        steps = target_y.shape[1]
        mean = self.mean(outputs)
        mean_target = mean[:, -steps:, :]
        mean_context = mean[:, :-steps, :]

        loss = None
        if target_y is not None:
            y = torch.cat([context_y, target_y], 1)
            y_mask = torch.isfinite(y) & (y != self.hparams.nan_value)
            y[~y_mask] = 0
            y = y.detach()

            loss_scale = 100
            # loss = F.mse_loss(mean * loss_scale, y * loss_scale, reduction='none') / loss_scale

            loss_target = (
                F.mse_loss(
                    mean_target * loss_scale,
                    y[:, -steps:, :] * loss_scale,
                    reduction="none",
                )
                / loss_scale
            )
            loss_context = (
                F.mse_loss(
                    mean_context * loss_scale,
                    y[:, :-steps, :] * loss_scale,
                    reduction="none",
                )
                / loss_scale
            )

            y_mask_target = y_mask[:, -steps:, :].detach()
            y_mask_context = y_mask[:, :-steps, :].detach()
            # loss_target = loss[:, -steps:, :]
            # loss_context = loss[:, :-steps, :]
            # print(0, loss_context.sum(), loss_target.sum())

            weight = (
                (torch.arange(loss_target.shape[1]) + 0.5)
                .float()
                .to(device)[None, :, None]
            )
            # weight /= weight.sum()
            # print(1.0, loss_context.sum(), loss_target.sum())
            loss_target = loss_target / torch.sqrt(
                weight
            )  # We want to weight nearer stuff more
            # print(1.5, loss_context.sum(), y_mask_context.sum(), loss_target.sum(), y_mask_target.sum(), (loss_context * y_mask_context).sum())
            loss_context = (loss_context * y_mask_context.float()).sum() / (
                y_mask_context.sum() + 1.0
            )
            loss_target = (loss_target * y_mask_target.float()).sum() / (
                y_mask_target.sum() + 1.0
            )  # Mean over unmasked ones
            # print(2, loss_context.sum(), loss_target.sum())

            # Perhaps predicting the past, as a secondary loss will help
            loss = loss_context / 100.0 + loss_target

            assert torch.isfinite(loss)

        return mean_target, dict(loss=loss), dict()


class PL_Transformer(PL_Seq2Seq):
    def __init__(self, hparams, MODEL_CLS=NetTransformer, **kwargs):
        super().__init__(hparams, MODEL_CLS=MODEL_CLS, **kwargs)

    DEFAULT_ARGS = {
        "attention_dropout": 0.4151003234623061,
        "hidden_out_size_power": 2.0,
        "hidden_size_power": 2.0,
        "learning_rate": 0.0026738884132767185,
        "nhead_power": 1.0,
        "nlayers": 2,
    }

    @staticmethod
    def add_suggest(trial: optuna.Trial, user_attrs={}):
        """
        Add hyperparam ranges to an optuna trial and typical user attrs.
        
        Usage:
            trial = optuna.trial.FixedTrial(
                params={         
                    'hidden_size': 128,
                }
            )
            trial = add_suggest(trial)
            trainer = pl.Trainer()
            model = LSTM_PL(dict(**trial.params, **trial.user_attrs), dataset_train,
                            dataset_test, cache_base_path, norm)
            trainer.fit(model)
        """
        trial.suggest_loguniform("learning_rate", 1e-5, 1e-2)
        trial.suggest_uniform("attention_dropout", 0, 0.9)
        trial.suggest_discrete_uniform("hidden_size_power", 2, 10, 1)
        trial.suggest_discrete_uniform("hidden_out_size_power", 2, 9, 1)
        trial.suggest_discrete_uniform("nhead_power", 1, 4, 1)
        trial.suggest_int("nlayers", 1, 12)

        user_attrs_default = {
            "batch_size": 16,
            "grad_clip": 40,
            "max_nb_epochs": 200,
            "num_workers": 4,
            "vis_i": 670,
            "x_dim": 6,
            "y_dim": 1,
            "nan_value": -99.9,
            "context_in_target": False,
            "patience": 3,
        }
        [trial.set_user_attr(k, v) for k, v in user_attrs_default.items()]
        [trial.set_user_attr(k, v) for k, v in user_attrs.items()]
        return trial
