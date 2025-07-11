import sys

sys.path.append("src")
import torch
import logging
import torch.nn as nn
from audioldm_finetuned.modules.clap.open_clip import create_model
from audioldm_finetuned.modules.clap.training.data import get_audio_features

import torchaudio
from transformers import (
    RobertaTokenizer,
    AutoTokenizer,
    T5EncoderModel,
    MT5EncoderModel,
)
import torch.nn.functional as F
from audioldm_finetuned.modules.audiomae.AudioMAE import Vanilla_AudioMAE
from audioldm_finetuned.modules.phoneme_encoder.encoder import TextEncoder

from transformers import SpeechT5Processor, AutoTokenizer, GPT2Model, GPT2Tokenizer
from transformers.models.speecht5.modeling_speecht5 import SpeechT5EncoderWithTextPrenet

from audioldm_finetuned.modules.audiomae.sequence_gen.model import CLAP2AudioMAE
from audioldm_finetuned.modules.audiomae.sequence_gen.sequence_input import (
    Sequence2AudioMAE,
)
import numpy as np
from audioldm_finetuned.modules.audiomae.sequence_gen.model import Prenet

"""
The model forward function can return three types of data:
1. tensor: used directly as conditioning signal
2. dict: where there is a main key as condition, there are also other key that you can use to pass loss function and itermediate result. etc.
3. list: the length is 2, in which the first element is tensor, the second element is attntion mask.

The output shape for the cross attention condition should be:
x,x_mask = [bs, seq_len, emb_dim], [bs, seq_len]

All the returned data, in which will be used as diffusion input, will need to be in float type
"""


class GPT2WordEmbedding(nn.Module):
    def __init__(self):
        super().__init__()
        # self.tokenizer = AutoTokenizer.from_pretrained("gpt2")
        self.tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = GPT2Model.from_pretrained("gpt2").wte
        self.device = None

    def get_unconditional_condition(self, batchsize):
        unconditional_condition = ["random"] * batchsize
        return self(unconditional_condition)

    def forward(self, text):
        assert isinstance(text, list)
        if self.device is None:
            self.device = next(self.model.parameters()).device

        tokenization_result = self.tokenizer(text, return_tensors="pt", padding=True)
        input_ids, attn_mask = tokenization_result["input_ids"].to(
            self.device
        ), tokenization_result["attention_mask"].to(self.device)

        input_embed = self.model(input_ids.long())

        return [input_embed, attn_mask]


class ConcateBandWidthCond(nn.Module):
    def __init__(self, latent_t_size, latent_f_size):
        super().__init__()
        self.placeholder = nn.Linear(1, 1)
        self.latent_t_size = latent_t_size
        self.latent_f_size = latent_f_size
        self.device = None

    def get_unconditional_condition(self, batchsize):
        return torch.zeros((batchsize, self.latent_t_size, self.latent_f_size)).to(
            self.device
        )

    def forward(self, mel_spec_bandwidth_cond_extra_channel):
        if self.device is None:
            self.device = mel_spec_bandwidth_cond_extra_channel.device

        return mel_spec_bandwidth_cond_extra_channel


class BandwidthEncoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.emb = nn.Embedding(1000, 128)
        nn.init.normal_(self.emb.weight, 0.0, 128**-0.5)
        self.linear_bandwidth = nn.Linear(128, 128)
        self.unconditional_condition = torch.zeros((1, 256))
        self.device = None

    def get_unconditional_condition(self, batchsize):
        return self.unconditional_condition.expand(batchsize, 256)

    def forward(self, bandwidth):

        if self.device is None:
            self.device = next(self.linear_bandwidth.parameters()).device
            self.unconditional_condition = self.unconditional_condition.to(self.device)

        # freq_energy_percentile
        lower_cutoff, higher_cutoff = bandwidth[..., 0], bandwidth[..., 1]
        # lower_cutoff, higher_cutoff = lower_cutoff*0+5, higher_cutoff*0+300

        lower_cutoff_emb = self.linear_bandwidth(self.emb(lower_cutoff.long()))
        higher_cutoff_emb = self.linear_bandwidth(self.emb(higher_cutoff.long()))
        cutoff_emb = torch.cat([lower_cutoff_emb, higher_cutoff_emb], dim=-1)
        # [bs, 256]
        return cutoff_emb


class SpeechT5TextEncoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.processor = SpeechT5Processor.from_pretrained("microsoft/speecht5_tts")
        self.model = SpeechT5EncoderWithTextPrenet.from_pretrained(
            "microsoft/speecht5_tts"
        )
        for p in self.model.parameters():
            p.requires_grad = False
        self.model.eval()

    # Required
    def get_unconditional_condition(self, batchsize):
        device = self.model.device
        hidden_state = torch.zeros((batchsize, 1, 768)).to(device)
        attention_mask = torch.ones((batchsize, 1)).to(device)
        return [hidden_state.float(), attention_mask.float()]

    def forward(self, text):
        with torch.no_grad():
            device = self.model.device
            inputs = self.processor(text=text, return_tensors="pt", padding=True)
            input_ids, attention_mask = inputs["input_ids"].to(device), inputs[
                "attention_mask"
            ].to(device)
            emb = self.model(input_ids, attention_mask)
            emb = emb.last_hidden_state.detach()
        return [emb.float(), attention_mask.float()]


class PhonemeEncoder(nn.Module):
    def __init__(self, vocabs_size=41, pad_length=250, pad_token_id=None):
        super().__init__()
        """
            encoder = PhonemeEncoder(40)
            data = torch.randint(0, 39, (2, 250))
            output = encoder(data)
            import ipdb;ipdb.set_trace()
        """
        assert pad_token_id is not None

        self.device = None
        self.PAD_LENGTH = int(pad_length)
        self.pad_token_id = pad_token_id
        self.pad_token_sequence = torch.tensor([self.pad_token_id] * self.PAD_LENGTH)

        self.text_encoder = TextEncoder(
            n_vocab=vocabs_size,
            out_channels=192,
            hidden_channels=192,
            filter_channels=768,
            n_heads=2,
            n_layers=6,
            kernel_size=3,
            p_dropout=0.1,
        )

        self.learnable_positional_embedding = torch.nn.Parameter(
            torch.zeros((1, 192, self.PAD_LENGTH))
        )  # [batchsize, seqlen, padlen]
        self.learnable_positional_embedding.requires_grad = True

    # Required
    def get_unconditional_condition(self, batchsize):
        unconditional_tokens = self.pad_token_sequence.expand(
            batchsize, self.PAD_LENGTH
        )
        return self(unconditional_tokens)  # Need to return float type

    # def get_unconditional_condition(self, batchsize):

    #     hidden_state = torch.zeros((batchsize, self.PAD_LENGTH, 192)).to(self.device)
    #     attention_mask = torch.ones((batchsize, self.PAD_LENGTH)).to(self.device)
    #     return [hidden_state, attention_mask] # Need to return float type

    def _get_src_mask(self, phoneme):
        src_mask = phoneme != self.pad_token_id
        return src_mask

    def _get_src_length(self, phoneme):
        src_mask = self._get_src_mask(phoneme)
        length = torch.sum(src_mask, dim=-1)
        return length

    # def make_empty_condition_unconditional(self, src_length, text_emb, attention_mask):
    #     # src_length: [bs]
    #     # text_emb: [bs, 192, pad_length]
    #     # attention_mask: [bs, pad_length]
    #     mask = src_length[..., None, None] > 1
    #     text_emb = text_emb * mask

    #     attention_mask[src_length < 1] = attention_mask[src_length < 1] * 0.0 + 1.0
    #     return text_emb, attention_mask

    def forward(self, phoneme_idx):
        if self.device is None:
            self.device = self.learnable_positional_embedding.device
            self.pad_token_sequence = self.pad_token_sequence.to(self.device)

        src_length = self._get_src_length(phoneme_idx)
        text_emb, m, logs, text_emb_mask = self.text_encoder(phoneme_idx, src_length)
        text_emb = text_emb + self.learnable_positional_embedding

        # text_emb, text_emb_mask = self.make_empty_condition_unconditional(src_length, text_emb, text_emb_mask)

        return [
            text_emb.permute(0, 2, 1),
            text_emb_mask.squeeze(1),
        ]  # [2, 250, 192], [2, 250]


class FlanT5HiddenState(nn.Module):
    """
    llama = FlanT5HiddenState()
    data = ["","this is not an empty sentence"]
    encoder_hidden_states = llama(data)
    import ipdb;ipdb.set_trace()
    """

    def __init__(
        self, text_encoder_name="google/flan-t5-large", freeze_text_encoder=True
    ):
        super().__init__()
        self.freeze_text_encoder = freeze_text_encoder
        self.tokenizer = AutoTokenizer.from_pretrained(text_encoder_name)
        self.model = T5EncoderModel.from_pretrained(text_encoder_name)
        if freeze_text_encoder:
            self.model.eval()
            for p in self.model.parameters():
                p.requires_grad = False
        else:
            print("=> The text encoder is learnable")

        self.empty_hidden_state_cfg = None
        self.device = None

    # Required
    def get_unconditional_condition(self, batchsize):
        param = next(self.model.parameters())
        if self.freeze_text_encoder:
            assert param.requires_grad == False

        # device = param.device
        if self.empty_hidden_state_cfg is None:
            self.empty_hidden_state_cfg, _ = self([""])

        hidden_state = torch.cat([self.empty_hidden_state_cfg] * batchsize).float()
        attention_mask = (
            torch.ones((batchsize, hidden_state.size(1)))
            .to(hidden_state.device)
            .float()
        )
        return [hidden_state, attention_mask]  # Need to return float type

    def forward(self, batch):
        param = next(self.model.parameters())
        if self.freeze_text_encoder:
            assert param.requires_grad == False

        if self.device is None:
            self.device = param.device

        # print("Manually change text")
        # for i in range(len(batch)):
        #     batch[i] = "dog barking"
        try:
            return self.encode_text(batch)
        except Exception as e:
            print(e, batch)
            logging.exception("An error occurred: %s", str(e))

    def encode_text(self, prompt):
        device = self.model.device
        batch = self.tokenizer(
            prompt,
            max_length=128,  # self.tokenizer.model_max_length
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        input_ids, attention_mask = batch.input_ids.to(device), batch.attention_mask.to(
            device
        )
        # Get text encoding
        if self.freeze_text_encoder:
            with torch.no_grad():
                encoder_hidden_states = self.model(
                    input_ids=input_ids, attention_mask=attention_mask
                )[0]
        else:
            encoder_hidden_states = self.model(
                input_ids=input_ids, attention_mask=attention_mask
            )[0]
        return [
            encoder_hidden_states.detach(),
            attention_mask.float(),
        ]  # Attention mask == 1 means usable token


class FlanT5HiddenStatePaddedSameLength(nn.Module):
    """
    llama = FlanT5HiddenState()
    data = ["","this is not an empty sentence"]
    encoder_hidden_states = llama(data)
    import ipdb;ipdb.set_trace()
    """

    def __init__(
        self, text_encoder_name="google/flan-t5-large", freeze_text_encoder=True
    ):
        super().__init__()
        self.freeze_text_encoder = freeze_text_encoder
        self.tokenizer = AutoTokenizer.from_pretrained(text_encoder_name)
        self.model = T5EncoderModel.from_pretrained(text_encoder_name)
        if freeze_text_encoder:
            self.model.eval()
            for p in self.model.parameters():
                p.requires_grad = False
        else:
            print("=> The text encoder is learnable")

        self.empty_hidden_state_cfg = None
        self.device = None

    # Required
    def get_unconditional_condition(self, batchsize):
        param = next(self.model.parameters())
        if self.freeze_text_encoder:
            assert param.requires_grad == False

        # device = param.device
        if self.empty_hidden_state_cfg is None:
            self.empty_hidden_state_cfg, _ = self([""])

        hidden_state = torch.cat([self.empty_hidden_state_cfg] * batchsize).float()
        attention_mask = (
            torch.ones((batchsize, hidden_state.size(1)))
            .to(hidden_state.device)
            .float()
        )
        return [hidden_state, attention_mask]  # Need to return float type

    def forward(self, batch):
        param = next(self.model.parameters())
        if self.freeze_text_encoder:
            assert param.requires_grad == False

        if self.device is None:
            self.device = param.device

        # print("Manually change text")
        # for i in range(len(batch)):
        #     batch[i] = "dog barking"
        try:
            text_embed = self.encode_text(batch)
            return text_embed
        except Exception as e:
            print(e, batch)
            logging.exception("An error occurred: %s", str(e))

    def encode_text(self, prompt):
        device = self.model.device
        batch = self.tokenizer(
            prompt,
            max_length=128,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        input_ids, attention_mask = batch.input_ids.to(device), batch.attention_mask.to(
            device
        )

        # Get text encoding
        if self.freeze_text_encoder:
            with torch.no_grad():
                encoder_hidden_states = self.model(
                    input_ids=input_ids, attention_mask=attention_mask
                )[0]
        else:
            encoder_hidden_states = self.model(
                input_ids=input_ids, attention_mask=attention_mask
            )[0]
        return [
            encoder_hidden_states.detach(),
            attention_mask.float(),
        ]  # Attention mask == 1 means usable token


class CLAPGenAudioMAECond(CLAP2AudioMAE):
    def __init__(
        self,
        cond_stage_config,
        learnable=True,
        pretrained_path=None,
        use_gt_mae_output=None,  # False: does not use AudioMAE GT, True: Use AudioMAE GT
        use_gt_mae_prob=None,
    ):  # The prob of using AudioMAE GT
        super().__init__(base_learning_rate=1e-5, cond_stage_config=cond_stage_config)
        assert use_gt_mae_output is not None and use_gt_mae_prob is not None

        if pretrained_path is not None:
            print("Reload CLAPGenAudioMAECond from %s" % pretrained_path)
            state_dict = torch.load(pretrained_path)["state_dict"]
            self.load_state_dict(state_dict)

        self.use_gt_mae_output = use_gt_mae_output
        self.use_gt_mae_prob = use_gt_mae_prob
        self.learnable = learnable

        if not learnable:
            # Only optimize the GPT2 model
            for p in self.model.parameters():
                p.requires_grad = False
            self.eval()

    # Required
    def get_unconditional_condition(self, batchsize):
        return_dict = self.cfg_uncond(batchsize)
        return return_dict

    def forward(self, batch):
        # The conditional module can return both tensor or dictionaries
        # The returned tensor will be corresponding to the cond_stage_key
        # The returned dict will have keys that correspond to the cond_stage_key
        ret_dict = {}
        if self.use_gt_mae_output and torch.rand(1).item() < self.use_gt_mae_prob:
            cond_dict = self.get_input(batch)
            # Used as condition
            ret_dict["crossattn_clap_to_audiomae_feature"] = [
                cond_dict["crossattn_audiomae_pooled"][0],
                torch.ones_like(cond_dict["crossattn_audiomae_pooled"][1]).float(),
            ]  # Input sequence and mask
        else:
            # Used as condition
            input_embeds, cond_dict = self.generate(batch)
            input_embeds_mask = (
                torch.ones((input_embeds.size(0), input_embeds.size(1)))
                .to(input_embeds.device)
                .float()
            )
            ret_dict["crossattn_clap_to_audiomae_feature"] = [
                input_embeds,
                input_embeds_mask,
            ]  # Input sequence and mask

        # If the following two keys are not in cond_stage_key, then they will not be used as condition
        ret_dict["film_clap_cond1"] = cond_dict[
            "film_clap_cond1"
        ]  # the clap target latent
        ret_dict["crossattn_audiomae_pooled"] = cond_dict[
            "crossattn_audiomae_pooled"
        ]  # audiomae target latent

        if self.learnable and self.training:
            loss = self.training_step(batch, cond_dict=cond_dict)
            ret_dict["noncond_loss_clap2audiomae"] = loss

        return ret_dict


class SequenceGenAudioMAECond(Sequence2AudioMAE):
    def __init__(
        self,
        cond_stage_config,
        base_learning_rate,
        sequence_gen_length,
        sequence_input_key,
        sequence_input_embed_dim,
        batchsize,
        always_output_audiomae_gt=False,
        pretrained_path=None,
        force_reload_pretrain_avoid_overwrite=False,
        learnable=True,
        use_warmup=True,
        use_gt_mae_output=None,  # False: does not use AudioMAE GT, True: Use AudioMAE GT
        use_gt_mae_prob=None,
    ):  # The prob of using AudioMAE GT
        if use_warmup:
            print(
                "Warning: You didn't initialize sequence prediction module with trainer. Set warmup to False. You can still use the warmup scheme from the latent diffusion model."
            )
            use_warmup = False

        super().__init__(
            base_learning_rate=base_learning_rate,
            cond_stage_config=cond_stage_config,
            sequence_gen_length=sequence_gen_length,
            sequence_input_key=sequence_input_key,
            use_warmup=use_warmup,
            sequence_input_embed_dim=sequence_input_embed_dim,
            batchsize=batchsize,
        )

        assert use_gt_mae_output is not None and use_gt_mae_prob is not None
        self.always_output_audiomae_gt = always_output_audiomae_gt
        self.force_reload_pretrain_avoid_overwrite = (
            force_reload_pretrain_avoid_overwrite
        )
        self.pretrained_path = pretrained_path
        if self.force_reload_pretrain_avoid_overwrite:
            self.is_reload = False
        else:
            self.is_reload = True

        self.load_pretrain_model()

        self.use_gt_mae_output = use_gt_mae_output
        self.use_gt_mae_prob = use_gt_mae_prob
        self.learnable = learnable

        if not learnable:
            # Only optimize the GPT2 model
            for p in self.model.parameters():
                p.requires_grad = False
            self.eval()

    def load_pretrain_model(self):
        if self.pretrained_path is not None:
            print("Reload SequenceGenAudioMAECond from %s" % self.pretrained_path)
            state_dict = torch.load(self.pretrained_path)["state_dict"]
            self.load_state_dict(state_dict)

    # Required
    def get_unconditional_condition(self, batchsize):
        return_dict = self.cfg_uncond(batchsize)
        return_dict["crossattn_audiomae_generated"] = [
            return_dict["crossattn_audiomae_pooled"][0],
            torch.ones_like(return_dict["crossattn_audiomae_pooled"][1]).float(),
        ]
        return return_dict

    def forward(self, batch):
        # The conditional module can return both tensor or dictionaries
        # The returned tensor will be corresponding to the cond_stage_key
        # The returned dict will have keys that correspond to the cond_stage_key
        ret_dict = {}

        if self.force_reload_pretrain_avoid_overwrite and not self.is_reload:
            self.load_pretrain_model()
            self.is_reload = True

        self.check_module_param_update()

        if self.always_output_audiomae_gt or (
            self.use_gt_mae_output and torch.rand(1).item() < self.use_gt_mae_prob
        ):
            cond_dict = self.get_input(batch)
            ret_dict["crossattn_audiomae_generated"] = [
                cond_dict["crossattn_audiomae_pooled"][0],
                torch.ones_like(cond_dict["crossattn_audiomae_pooled"][1]).float(),
            ]  # Input sequence and mask
            # _, output = self.training_step(batch, cond_dict=cond_dict, return_output=True)
            # ret_dict["crossattn_audiomae_generated"] = [output, torch.ones_like(cond_dict["crossattn_audiomae_pooled"][1]).float()] # Input sequence and mask
        else:
            if not self.training:
                print("--------------> Generate !!!!!!!!!!!!")
            input_embeds, cond_dict = self.generate(batch)
            # print("Generate Partial!!!!"); input_embeds, cond_dict = self.generate_partial(batch)
            input_embeds_mask = (
                torch.ones((input_embeds.size(0), input_embeds.size(1)))
                .to(input_embeds.device)
                .float()
            )
            ret_dict["crossattn_audiomae_generated"] = [
                input_embeds,
                input_embeds_mask,
            ]  # Input sequence and mask

        # If the following two keys are not in cond_stage_key, then they will not be used as condition
        for key in cond_dict.keys():
            ret_dict[key] = cond_dict[key]

        if self.learnable and self.training:
            loss = self.training_step(batch, cond_dict=cond_dict)
            ret_dict["noncond_loss_clap2audiomae"] = loss

        return ret_dict


class SequenceGenAudioMAECond_AudioMAE_PostNet(Sequence2AudioMAE):
    def __init__(
        self,
        cond_stage_config,
        base_learning_rate,
        sequence_gen_length,
        sequence_input_key,
        sequence_input_embed_dim,
        batchsize,
        always_output_audiomae_gt=False,
        pretrained_path=None,
        use_ar_gen_loss=False,
        force_reload_pretrain_avoid_overwrite=False,
        learnable=True,
        use_warmup=True,
        use_gt_mae_output=None,  # False: does not use AudioMAE GT, True: Use AudioMAE GT
        use_gt_mae_prob=None,
    ):  # The prob of using AudioMAE GT
        if use_warmup:
            print(
                "Warning: You didn't initialize sequence prediction module with trainer. Set warmup to False. You can still use the warmup scheme from the latent diffusion model."
            )
            use_warmup = False

        super().__init__(
            base_learning_rate=base_learning_rate,
            cond_stage_config=cond_stage_config,
            sequence_gen_length=sequence_gen_length,
            sequence_input_key=sequence_input_key,
            use_ar_gen_loss=use_ar_gen_loss,
            use_warmup=use_warmup,
            sequence_input_embed_dim=sequence_input_embed_dim,
            batchsize=batchsize,
        )

        assert use_gt_mae_output is not None and use_gt_mae_prob is not None
        self.always_output_audiomae_gt = always_output_audiomae_gt
        self.force_reload_pretrain_avoid_overwrite = (
            force_reload_pretrain_avoid_overwrite
        )
        self.pretrained_path = pretrained_path
        if self.force_reload_pretrain_avoid_overwrite:
            self.is_reload = False
        else:
            self.is_reload = True

        self.load_pretrain_model()

        self.prenet = Prenet(in_dim=768, sizes=[768, 768, 768], dropout_rate=0.5)

        self.use_gt_mae_output = use_gt_mae_output
        self.use_gt_mae_prob = use_gt_mae_prob
        self.learnable = learnable

        if not learnable:
            # Only optimize the GPT2 model
            for p in self.model.parameters():
                p.requires_grad = False
            self.eval()

    def load_pretrain_model(self):
        if self.pretrained_path is not None:
            print("Reload SequenceGenAudioMAECond from %s" % self.pretrained_path)
            state_dict = torch.load(self.pretrained_path)["state_dict"]
            self.load_state_dict(state_dict)

    # Required
    def get_unconditional_condition(self, batchsize):
        return_dict = self.cfg_uncond(batchsize)
        return_dict["crossattn_audiomae_generated"] = [
            return_dict["crossattn_audiomae_pooled"][0],
            torch.ones_like(return_dict["crossattn_audiomae_pooled"][1]).float(),
        ]
        return return_dict

    def forward(self, batch):
        # The conditional module can return both tensor or dictionaries
        # The returned tensor will be corresponding to the cond_stage_key
        # The returned dict will have keys that correspond to the cond_stage_key
        ret_dict = {}

        if self.force_reload_pretrain_avoid_overwrite and not self.is_reload:
            self.load_pretrain_model()
            self.is_reload = True

        self.check_module_param_update()

        if self.always_output_audiomae_gt or (
            self.use_gt_mae_output and torch.rand(1).item() < self.use_gt_mae_prob
        ):
            cond_dict = self.get_input(batch)
            gt_audiomae = self.prenet(cond_dict["crossattn_audiomae_pooled"][0])
            ret_dict["crossattn_audiomae_generated"] = [
                gt_audiomae,
                torch.ones_like(cond_dict["crossattn_audiomae_pooled"][1]).float(),
            ]  # Input sequence and mask
        else:
            print("--------------> Generate!!!!!!!!!!!!")
            input_embeds, cond_dict = self.generate(batch)
            # input_embeds, cond_dict = self.generate_partial(batch)
            input_embeds = self.prenet(input_embeds)
            input_embeds_mask = (
                torch.ones((input_embeds.size(0), input_embeds.size(1)))
                .to(input_embeds.device)
                .float()
            )
            ret_dict["crossattn_audiomae_generated"] = [
                input_embeds,
                input_embeds_mask,
            ]  # Input sequence and mask

        # If the following two keys are not in cond_stage_key, then they will not be used as condition
        for key in cond_dict.keys():
            ret_dict[key] = cond_dict[key]

        if self.learnable and self.training:
            loss = self.training_step(batch, cond_dict=cond_dict)
            ret_dict["noncond_loss_clap2audiomae"] = loss

        return ret_dict


class AudioMAEConditionCTPoolRandTFSeparated(nn.Module):
    """
    audiomae = AudioMAEConditionCTPool2x2()
    data = torch.randn((4, 1024, 128))
    output = audiomae(data)
    import ipdb;ipdb.set_trace()
    exit(0)
    """

    def __init__(
        self,
        time_pooling_factors=[1, 2, 4, 8],
        freq_pooling_factors=[1, 2, 4, 8],
        eval_time_pooling=None,
        eval_freq_pooling=None,
        mask_ratio=0.0,
        regularization=False,
        no_audiomae_mask=True,
        no_audiomae_average=False,
    ):
        super().__init__()
        self.device = None
        self.time_pooling_factors = time_pooling_factors
        self.freq_pooling_factors = freq_pooling_factors
        self.no_audiomae_mask = no_audiomae_mask
        self.no_audiomae_average = no_audiomae_average

        self.eval_freq_pooling = eval_freq_pooling
        self.eval_time_pooling = eval_time_pooling
        self.mask_ratio = mask_ratio
        self.use_reg = regularization

        self.audiomae = Vanilla_AudioMAE()
        self.audiomae.eval()
        for p in self.audiomae.parameters():
            p.requires_grad = False

    # Required
    def get_unconditional_condition(self, batchsize):
        param = next(self.audiomae.parameters())
        assert param.requires_grad == False
        device = param.device
        # time_pool, freq_pool = max(self.time_pooling_factors), max(self.freq_pooling_factors)
        time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
            self.eval_freq_pooling, 8
        )
        # time_pool = self.time_pooling_factors[np.random.choice(list(range(len(self.time_pooling_factors))))]
        # freq_pool = self.freq_pooling_factors[np.random.choice(list(range(len(self.freq_pooling_factors))))]
        token_num = int(512 / (time_pool * freq_pool))
        return [
            torch.zeros((batchsize, token_num, 768)).to(device).float(),
            torch.ones((batchsize, token_num)).to(device).float(),
        ]

    def pool(self, representation, time_pool=None, freq_pool=None):
        assert representation.size(-1) == 768
        representation = representation[:, 1:, :].transpose(1, 2)
        bs, embedding_dim, token_num = representation.size()
        representation = representation.reshape(bs, embedding_dim, 64, 8)

        if self.training:
            if time_pool is None and freq_pool is None:
                time_pool = min(
                    64,
                    self.time_pooling_factors[
                        np.random.choice(list(range(len(self.time_pooling_factors))))
                    ],
                )
                freq_pool = min(
                    8,
                    self.freq_pooling_factors[
                        np.random.choice(list(range(len(self.freq_pooling_factors))))
                    ],
                )
                # freq_pool = min(8, time_pool) # TODO here I make some modification.
        else:
            time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
                self.eval_freq_pooling, 8
            )

        self.avgpooling = nn.AvgPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )
        self.maxpooling = nn.MaxPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )

        pooled = (
            self.avgpooling(representation) + self.maxpooling(representation)
        ) / 2  # [bs, embedding_dim, time_token_num, freq_token_num]
        pooled = pooled.flatten(2).transpose(1, 2)
        return pooled  # [bs, token_num, embedding_dim]

    def regularization(self, x):
        assert x.size(-1) == 768
        x = F.normalize(x, p=2, dim=-1)
        return x

    # Required
    def forward(self, batch, time_pool=None, freq_pool=None):
        assert batch.size(-2) == 1024 and batch.size(-1) == 128

        if self.device is None:
            self.device = batch.device

        batch = batch.unsqueeze(1)
        with torch.no_grad():
            representation = self.audiomae(
                batch,
                mask_ratio=self.mask_ratio,
                no_mask=self.no_audiomae_mask,
                no_average=self.no_audiomae_average,
            )
            representation = self.pool(representation, time_pool, freq_pool)
            if self.use_reg:
                representation = self.regularization(representation)
            return [
                representation,
                torch.ones((representation.size(0), representation.size(1)))
                .to(representation.device)
                .float(),
            ]


class AudioMAEConditionCTPoolRand(nn.Module):
    """
    audiomae = AudioMAEConditionCTPool2x2()
    data = torch.randn((4, 1024, 128))
    output = audiomae(data)
    import ipdb;ipdb.set_trace()
    exit(0)
    """

    def __init__(
        self,
        time_pooling_factors=[1, 2, 4, 8],
        freq_pooling_factors=[1, 2, 4, 8],
        eval_time_pooling=None,
        eval_freq_pooling=None,
        mask_ratio=0.0,
        regularization=False,
        no_audiomae_mask=True,
        no_audiomae_average=False,
    ):
        super().__init__()
        self.device = None
        self.time_pooling_factors = time_pooling_factors
        self.freq_pooling_factors = freq_pooling_factors
        self.no_audiomae_mask = no_audiomae_mask
        self.no_audiomae_average = no_audiomae_average

        self.eval_freq_pooling = eval_freq_pooling
        self.eval_time_pooling = eval_time_pooling
        self.mask_ratio = mask_ratio
        self.use_reg = regularization

        self.audiomae = Vanilla_AudioMAE()
        self.audiomae.eval()
        for p in self.audiomae.parameters():
            p.requires_grad = False

    # Required
    def get_unconditional_condition(self, batchsize):
        param = next(self.audiomae.parameters())
        assert param.requires_grad == False
        device = param.device
        # time_pool, freq_pool = max(self.time_pooling_factors), max(self.freq_pooling_factors)
        time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
            self.eval_freq_pooling, 8
        )
        # time_pool = self.time_pooling_factors[np.random.choice(list(range(len(self.time_pooling_factors))))]
        # freq_pool = self.freq_pooling_factors[np.random.choice(list(range(len(self.freq_pooling_factors))))]
        token_num = int(512 / (time_pool * freq_pool))
        return [
            torch.zeros((batchsize, token_num, 768)).to(device).float(),
            torch.ones((batchsize, token_num)).to(device).float(),
        ]

    def pool(self, representation, time_pool=None, freq_pool=None):
        assert representation.size(-1) == 768
        representation = representation[:, 1:, :].transpose(1, 2)
        bs, embedding_dim, token_num = representation.size()
        representation = representation.reshape(bs, embedding_dim, 64, 8)

        if self.training:
            if time_pool is None and freq_pool is None:
                time_pool = min(
                    64,
                    self.time_pooling_factors[
                        np.random.choice(list(range(len(self.time_pooling_factors))))
                    ],
                )
                # freq_pool = self.freq_pooling_factors[np.random.choice(list(range(len(self.freq_pooling_factors))))]
                freq_pool = min(8, time_pool)  # TODO here I make some modification.
        else:
            time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
                self.eval_freq_pooling, 8
            )

        self.avgpooling = nn.AvgPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )
        self.maxpooling = nn.MaxPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )

        pooled = (
            self.avgpooling(representation) + self.maxpooling(representation)
        ) / 2  # [bs, embedding_dim, time_token_num, freq_token_num]
        pooled = pooled.flatten(2).transpose(1, 2)
        return pooled  # [bs, token_num, embedding_dim]

    def regularization(self, x):
        assert x.size(-1) == 768
        x = F.normalize(x, p=2, dim=-1)
        return x

    # Required
    def forward(self, batch, time_pool=None, freq_pool=None):
        assert batch.size(-2) == 1024 and batch.size(-1) == 128

        if self.device is None:
            self.device = batch.device

        batch = batch.unsqueeze(1)
        with torch.no_grad():
            representation = self.audiomae(
                batch,
                mask_ratio=self.mask_ratio,
                no_mask=self.no_audiomae_mask,
                no_average=self.no_audiomae_average,
            )
            representation = self.pool(representation, time_pool, freq_pool)
            if self.use_reg:
                representation = self.regularization(representation)
            return [
                representation,
                torch.ones((representation.size(0), representation.size(1)))
                .to(representation.device)
                .float(),
            ]


class ConditionalToken(nn.Module):
    def __init__(self, embedding_dim):
        super(ConditionalToken, self).__init__()
        self.embedding_dim = embedding_dim
        # Define the conditional tokens as fixed values
        self.pooling_factor_tokens = {
            1: torch.Tensor([1.0, 0.0] * (embedding_dim // 2)),
            2: torch.Tensor([0.0, 1.0] * (embedding_dim // 2)),
            4: torch.Tensor([1.0, 1.0] * (embedding_dim // 2)),
            8: torch.Tensor([-1.0, 0.0] * (embedding_dim // 2)),
            16: torch.Tensor([0.0, -1.0] * (embedding_dim // 2)),
            32: torch.Tensor([-1.0, -1.0] * (embedding_dim // 2)),
            64: torch.Tensor([0.0, 0.0] * (embedding_dim // 2)),
        }
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, condition, batchsize):
        """
        Returns the conditional token for the given condition.
        """
        if condition not in self.pooling_factor_tokens.keys():
            raise ValueError(f"Unsupported condition: {condition}")
        batched_token = self.pooling_factor_tokens[condition][None, None].expand(
            batchsize, 1, self.embedding_dim
        )
        return batched_token


class AudioMAEConditionCTPoolRandV2(nn.Module):
    """
    audiomae = AudioMAEConditionCTPool2x2()
    data = torch.randn((4, 1024, 128))
    output = audiomae(data)
    import ipdb;ipdb.set_trace()
    exit(0)
    """

    def __init__(
        self,
        time_pooling_factors=[1, 2, 4, 8],
        freq_pooling_factors=[1, 2, 4, 8],
        eval_time_pooling=None,
        eval_freq_pooling=None,
        mask_ratio=0.0,
        regularization=False,
        no_audiomae_mask=True,
        no_audiomae_average=False,
    ):
        super().__init__()
        self.device = None
        self.time_pooling_factors = time_pooling_factors
        self.freq_pooling_factors = freq_pooling_factors
        self.no_audiomae_mask = no_audiomae_mask
        self.no_audiomae_average = no_audiomae_average

        self.eval_freq_pooling = eval_freq_pooling
        self.eval_time_pooling = eval_time_pooling
        self.mask_ratio = mask_ratio
        self.use_reg = regularization

        self.pooling_tokens = ConditionalToken(768)

        self.audiomae = Vanilla_AudioMAE()
        self.audiomae.eval()

        for p in self.audiomae.parameters():
            p.requires_grad = False

    # Required
    def get_unconditional_condition(self, batchsize):
        param = next(self.audiomae.parameters())
        assert param.requires_grad == False
        device = param.device
        # time_pool, freq_pool = max(self.time_pooling_factors), max(self.freq_pooling_factors)
        time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
            self.eval_freq_pooling, 8
        )
        # time_pool = self.time_pooling_factors[np.random.choice(list(range(len(self.time_pooling_factors))))]
        # freq_pool = self.freq_pooling_factors[np.random.choice(list(range(len(self.freq_pooling_factors))))]
        pool_condition_token = self.pooling_tokens(time_pool, batchsize).to(device)
        token_num = int(512 / (time_pool * freq_pool))

        rep = torch.zeros((batchsize, token_num, 768)).to(device).float()
        rep = torch.cat([rep, pool_condition_token], dim=1)

        return [rep, torch.ones((batchsize, token_num + 1)).to(device).float()]

    def pool(self, representation, time_pool=None, freq_pool=None):
        assert representation.size(-1) == 768
        representation = representation[:, 1:, :].transpose(1, 2)
        bs, embedding_dim, token_num = representation.size()
        representation = representation.reshape(bs, embedding_dim, 64, 8)

        if self.training:
            if time_pool is None and freq_pool is None:
                time_pool = min(
                    64,
                    self.time_pooling_factors[
                        np.random.choice(list(range(len(self.time_pooling_factors))))
                    ],
                )
                # freq_pool = self.freq_pooling_factors[np.random.choice(list(range(len(self.freq_pooling_factors))))]
                freq_pool = min(8, time_pool)  # TODO here I make some modification.
        else:
            time_pool, freq_pool = min(self.eval_time_pooling, 64), min(
                self.eval_freq_pooling, 8
            )

        self.avgpooling = nn.AvgPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )
        self.maxpooling = nn.MaxPool2d(
            kernel_size=(time_pool, freq_pool), stride=(time_pool, freq_pool)
        )
        pooled = (
            self.avgpooling(representation) + self.maxpooling(representation)
        ) / 2  # [bs, embedding_dim, time_token_num, freq_token_num]
        pooled = pooled.flatten(2).transpose(1, 2)
        return pooled, time_pool, freq_pool  # [bs, token_num, embedding_dim]

    def regularization(self, x):
        assert x.size(-1) == 768
        x = F.normalize(x, p=2, dim=-1)
        return x

    # Required
    def forward(self, batch):
        assert batch.size(-2) == 1024 and batch.size(-1) == 128

        if self.device is None:
            self.device = batch.device

        batch = batch.unsqueeze(1)

        with torch.no_grad():
            representation = self.audiomae(
                batch,
                mask_ratio=self.mask_ratio,
                no_mask=self.no_audiomae_mask,
                no_average=self.no_audiomae_average,
            )
            representation, time_pool, freq_pool = self.pool(representation)
            if self.use_reg:
                representation = self.regularization(representation)
            pool_condition_token = self.pooling_tokens(
                time_pool, representation.size(0)
            ).to(representation.device)
            representation = torch.cat([representation, pool_condition_token], dim=1)

            return [
                representation,
                torch.ones((representation.size(0), representation.size(1)))
                .to(representation.device)
                .float(),
            ]


class BeatDownbeatConditionConcat(nn.Module):
    def __init__(self, latent_t_size, latent_f_size):
        super().__init__()
        self.latent_t_size = latent_t_size
        self.latent_f_size = latent_f_size
        self.device = None

    # Required
    def get_unconditional_condition(self, batchsize):
        return torch.zeros((batchsize, self.latent_t_size, self.latent_f_size)).to(
            self.device
        )

    # Required
    def forward(self, batch):
        if self.device is None:
            self.device = batch.device
        return batch


class CLAPAudioEmbeddingClassifierFreev2(nn.Module):
    def __init__(
        self,
        pretrained_path,
        sampling_rate=16000,
        embed_mode="audio",
        amodel="HTSAT-base",
        unconditional_prob=0.1,
        random_mute=False,
        max_random_mute_portion=0.5,
        training_mode=True,
    ):
        super().__init__()
        self.device = "cpu"
        self.precision = "fp32"
        self.amodel = amodel  # or 'PANN-14'
        self.tmodel = "roberta"  # the best text encoder in our training
        self.enable_fusion = False  # False if you do not want to use the fusion model
        self.fusion_type = "aff_2d"
        self.pretrained = pretrained_path
        self.embed_mode = embed_mode
        self.embed_mode_orig = embed_mode
        self.sampling_rate = sampling_rate
        self.unconditional_prob = unconditional_prob
        self.random_mute = random_mute
        self.tokenize = RobertaTokenizer.from_pretrained("roberta-base")
        self.max_random_mute_portion = max_random_mute_portion
        self.training_mode = training_mode
        self.model, self.model_cfg = create_model(
            self.amodel,
            self.tmodel,
            self.pretrained,
            precision=self.precision,
            device=self.device,
            enable_fusion=self.enable_fusion,
            fusion_type=self.fusion_type,
        )
        audio_cfg = self.model_cfg["audio_cfg"]
        self.mel_transform = torchaudio.transforms.MelSpectrogram(
            sample_rate=audio_cfg["sample_rate"],
            n_fft=audio_cfg["window_size"],
            win_length=audio_cfg["window_size"],
            hop_length=audio_cfg["hop_size"],
            center=True,
            pad_mode="reflect",
            power=2.0,
            norm=None,
            onesided=True,
            n_mels=64,
            f_min=audio_cfg["fmin"],
            f_max=audio_cfg["fmax"],
        )
        for p in self.model.parameters():
            p.requires_grad = False
        self.unconditional_token = None
        self.model.eval()

    def get_unconditional_condition(self, batchsize):
        self.unconditional_token = self.model.get_text_embedding(
            self.tokenizer(["", ""])
        )[0:1]
        return torch.cat([self.unconditional_token.unsqueeze(0)] * batchsize, dim=0)

    def batch_to_list(self, batch):
        ret = []
        for i in range(batch.size(0)):
            ret.append(batch[i])
        return ret

    def make_decision(self, probability):
        if float(torch.rand(1)) < probability:
            return True
        else:
            return False

    def random_uniform(self, start, end):
        val = torch.rand(1).item()
        return start + (end - start) * val

    def _random_mute(self, waveform):
        # waveform: [bs, t-steps]
        t_steps = waveform.size(-1)
        for i in range(waveform.size(0)):
            mute_size = int(
                self.random_uniform(0, end=int(t_steps * self.max_random_mute_portion))
            )
            mute_start = int(self.random_uniform(0, t_steps - mute_size))
            waveform[i, mute_start : mute_start + mute_size] = 0
        return waveform

    def cos_similarity(self, waveform, text):
        # waveform: [bs, t_steps]
        original_embed_mode = self.embed_mode
        with torch.no_grad():
            self.embed_mode = "audio"
            audio_emb = self(waveform.cuda())
            self.embed_mode = "text"
            text_emb = self(text)
            similarity = F.cosine_similarity(audio_emb, text_emb, dim=2)
        self.embed_mode = original_embed_mode
        return similarity.squeeze()

    def build_unconditional_emb(self):
        self.unconditional_token = self.model.get_text_embedding(
            self.tokenizer(["", ""])
        )[0:1]

    def forward(self, batch):
        # If you want this conditioner to be unconditional, set self.unconditional_prob = 1.0
        # If you want this conditioner to be fully conditional, set self.unconditional_prob = 0.0
        if self.model.training == True and not self.training_mode:
            print(
                "The pretrained CLAP model should always be in eval mode. Reloading model just in case you change the parameters."
            )
            self.model, self.model_cfg = create_model(
                self.amodel,
                self.tmodel,
                self.pretrained,
                precision=self.precision,
                device="cuda",
                enable_fusion=self.enable_fusion,
                fusion_type=self.fusion_type,
            )
            for p in self.model.parameters():
                p.requires_grad = False
            self.model.eval()

        if self.unconditional_token is None:
            self.build_unconditional_emb()

        # if(self.training_mode):
        #     assert self.model.training == True
        # else:
        #     assert self.model.training == False

        # the 'fusion' truncate mode can be changed to 'rand_trunc' if run in unfusion mode
        if self.embed_mode == "audio":
            if not self.training:
                print("INFO: clap model calculate the audio embedding as condition")
            with torch.no_grad():
                # assert (
                #     self.sampling_rate == 16000
                # ), "We only support 16000 sampling rate"

                # if self.random_mute:
                #     batch = self._random_mute(batch)
                # batch: [bs, 1, t-samples]
                if self.sampling_rate != 48000:
                    batch = torchaudio.functional.resample(
                        batch, orig_freq=self.sampling_rate, new_freq=48000
                    )

                audio_data = batch.squeeze(1)
                mel = self.mel_transform(audio_data)
                audio_dict = get_audio_features(
                    audio_data,
                    mel,
                    480000,
                    data_truncating="fusion",
                    data_filling="repeatpad",
                    audio_cfg=self.model_cfg["audio_cfg"],
                )
                # [bs, 512]
                embed = self.model.get_audio_embedding(audio_dict)
        elif self.embed_mode == "text":
            with torch.no_grad():
                # the 'fusion' truncate mode can be changed to 'rand_trunc' if run in unfusion mode
                text_data = self.tokenizer(batch)

                if isinstance(batch, str) or (
                    isinstance(batch, list) and len(batch) == 1
                ):
                    for key in text_data.keys():
                        text_data[key] = text_data[key].unsqueeze(0)

                embed = self.model.get_text_embedding(text_data)

        embed = embed.unsqueeze(1)
        for i in range(embed.size(0)):
            if self.make_decision(self.unconditional_prob):
                embed[i] = self.unconditional_token
        # embed = torch.randn((batch.size(0), 1, 512)).type_as(batch)
        return embed.detach()

    def tokenizer(self, text):
        result = self.tokenize(
            text,
            padding="max_length",
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        return {k: v.squeeze(0) for k, v in result.items()}


if __name__ == "__main__":
    model = CLAPAudioEmbeddingClassifierFreev2(
        pretrained_path="/mnt/bn/lqhaoheliu/exps/checkpoints/audioldm_finetuned/ckpt/CLAP.pt",
        embed_mode="text",
        amodel="HTSAT-tiny",
    )
    # data = torch.randn((6, 1, int(16000*10.24)))
    data = ["text", "text"]
    res = model(data)
    import ipdb

    ipdb.set_trace()
