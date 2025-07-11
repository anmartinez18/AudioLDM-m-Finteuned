from email.policy import strict
import torch
import os

import pytorch_lightning as pl
import torch.nn.functional as F
from contextlib import contextmanager
import numpy as np
from audioldm_finetuned.modules.diffusionmodules.ema import *

from torch.optim.lr_scheduler import LambdaLR
from audioldm_finetuned.modules.diffusionmodules.model import Encoder, Decoder
from audioldm_finetuned.modules.diffusionmodules.distributions import (
    DiagonalGaussianDistribution,
)

import wandb
from audioldm_finetuned.utilities.model_util import instantiate_from_config
import soundfile as sf

from audioldm_finetuned.utilities.model_util import get_vocoder
from audioldm_finetuned.utilities.tools import synth_one_sample
import itertools


class AutoencoderKL(pl.LightningModule):
    def __init__(
        self,
        ddconfig=None,
        lossconfig=None,
        batchsize=None,
        embed_dim=None,
        time_shuffle=1,
        subband=1,
        sampling_rate=16000,
        ckpt_path=None,
        reload_from_ckpt=None,
        ignore_keys=[],
        image_key="fbank",
        colorize_nlabels=None,
        monitor=None,
        base_learning_rate=1e-5,
    ):
        super().__init__()
        self.automatic_optimization = False
        assert (
            "mel_bins" in ddconfig.keys()
        ), "mel_bins is not specified in the Autoencoder config"
        num_mel = ddconfig["mel_bins"]
        self.image_key = image_key
        self.sampling_rate = sampling_rate
        self.encoder = Encoder(**ddconfig)
        self.decoder = Decoder(**ddconfig)

        self.loss = instantiate_from_config(lossconfig)
        self.subband = int(subband)

        if self.subband > 1:
            print("Use subband decomposition %s" % self.subband)

        assert ddconfig["double_z"]
        self.quant_conv = torch.nn.Conv2d(2 * ddconfig["z_channels"], 2 * embed_dim, 1)
        self.post_quant_conv = torch.nn.Conv2d(embed_dim, ddconfig["z_channels"], 1)

        if self.image_key == "fbank":
            self.vocoder = get_vocoder(None, "cpu", num_mel)
        self.embed_dim = embed_dim
        if colorize_nlabels is not None:
            assert type(colorize_nlabels) == int
            self.register_buffer("colorize", torch.randn(3, colorize_nlabels, 1, 1))
        if monitor is not None:
            self.monitor = monitor
        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys=ignore_keys)
        self.learning_rate = float(base_learning_rate)
        print("Initial learning rate %s" % self.learning_rate)

        self.time_shuffle = time_shuffle
        self.reload_from_ckpt = reload_from_ckpt
        self.reloaded = False
        self.mean, self.std = None, None

        self.feature_cache = None
        self.flag_first_run = True
        self.train_step = 0

        self.logger_save_dir = None
        self.logger_exp_name = None
        self.logger_exp_group_name = None

        if not self.reloaded and self.reload_from_ckpt is not None:
            print("--> Reload weight of autoencoder from %s" % self.reload_from_ckpt)
            checkpoint = torch.load(self.reload_from_ckpt)

            load_todo_keys = {}
            pretrained_state_dict = checkpoint["state_dict"]
            current_state_dict = self.state_dict()
            for key in current_state_dict:
                if (
                    key in pretrained_state_dict.keys()
                    and pretrained_state_dict[key].size()
                    == current_state_dict[key].size()
                ):
                    load_todo_keys[key] = pretrained_state_dict[key]
                else:
                    print("Key %s mismatch during loading, seems fine" % key)

            self.load_state_dict(load_todo_keys, strict=False)
            self.reloaded = True
        else:
            print("Train from scratch")

    def get_log_dir(self):
        return os.path.join(
            self.logger_save_dir, self.logger_exp_group_name, self.logger_exp_name
        )

    def set_log_dir(self, save_dir, exp_group_name, exp_name):
        self.logger_save_dir = save_dir
        self.logger_exp_name = exp_name
        self.logger_exp_group_name = exp_group_name

    def init_from_ckpt(self, path, ignore_keys=list()):
        sd = torch.load(path, map_location="cpu")["state_dict"]
        keys = list(sd.keys())
        for k in keys:
            for ik in ignore_keys:
                if k.startswith(ik):
                    print("Deleting key {} from state_dict.".format(k))
                    del sd[k]
        self.load_state_dict(sd, strict=False)
        print(f"Restored from {path}")

    def encode(self, x):
        # x = self.time_shuffle_operation(x)
        x = self.freq_split_subband(x)
        h = self.encoder(x)
        moments = self.quant_conv(h)
        posterior = DiagonalGaussianDistribution(moments)
        return posterior

    def decode(self, z):
        z = self.post_quant_conv(z)
        dec = self.decoder(z)
        # bs, ch, shuffled_timesteps, fbins = dec.size()
        # dec = self.time_unshuffle_operation(dec, bs, int(ch*shuffled_timesteps), fbins)
        dec = self.freq_merge_subband(dec)
        return dec

    def decode_to_waveform(self, dec):
        from audioldm_finetuned.utilities.model_util import vocoder_infer

        if self.image_key == "fbank":
            dec = dec.squeeze(1).permute(0, 2, 1)
            wav_reconstruction = vocoder_infer(dec, self.vocoder)
        elif self.image_key == "stft":
            dec = dec.squeeze(1).permute(0, 2, 1)
            wav_reconstruction = self.wave_decoder(dec)
        return wav_reconstruction

    def visualize_latent(self, input):
        import matplotlib.pyplot as plt

        # for i in range(10):
        #     zero_input = torch.zeros_like(input) - 11.59
        #     zero_input[:,:,i * 16: i * 16 + 16,:16] += 13.59

        #     posterior = self.encode(zero_input)
        #     latent = posterior.sample()
        #     avg_latent = torch.mean(latent, dim=1)[0]
        #     plt.imshow(avg_latent.cpu().detach().numpy().T)
        #     plt.savefig("%s.png" % i)
        #     plt.close()

        np.save("input.npy", input.cpu().detach().numpy())
        # zero_input = torch.zeros_like(input) - 11.59
        time_input = input.clone()
        time_input[:, :, :, :32] *= 0
        time_input[:, :, :, :32] -= 11.59

        np.save("time_input.npy", time_input.cpu().detach().numpy())

        posterior = self.encode(time_input)
        latent = posterior.sample()
        np.save("time_latent.npy", latent.cpu().detach().numpy())
        avg_latent = torch.mean(latent, dim=1)
        for i in range(avg_latent.size(0)):
            plt.imshow(avg_latent[i].cpu().detach().numpy().T)
            plt.savefig("freq_%s.png" % i)
            plt.close()

        freq_input = input.clone()
        freq_input[:, :, :512, :] *= 0
        freq_input[:, :, :512, :] -= 11.59

        np.save("freq_input.npy", freq_input.cpu().detach().numpy())

        posterior = self.encode(freq_input)
        latent = posterior.sample()
        np.save("freq_latent.npy", latent.cpu().detach().numpy())
        avg_latent = torch.mean(latent, dim=1)
        for i in range(avg_latent.size(0)):
            plt.imshow(avg_latent[i].cpu().detach().numpy().T)
            plt.savefig("time_%s.png" % i)
            plt.close()

    def forward(self, input, sample_posterior=True):
        posterior = self.encode(input)
        if sample_posterior:
            z = posterior.sample()
        else:
            z = posterior.mode()

        if self.flag_first_run:
            print("Latent size: ", z.size())
            self.flag_first_run = False

        dec = self.decode(z)

        return dec, posterior

    def get_input(self, batch):
        fname, text, label_indices, waveform, stft, fbank = (
            batch["fname"],
            batch["text"],
            batch["label_vector"],
            batch["waveform"],
            batch["stft"],
            batch["log_mel_spec"],
        )
        # if(self.time_shuffle != 1):
        #     if(fbank.size(1) % self.time_shuffle != 0):
        #         pad_len = self.time_shuffle - (fbank.size(1) % self.time_shuffle)
        #         fbank = torch.nn.functional.pad(fbank, (0,0,0,pad_len))

        ret = {}

        ret["fbank"], ret["stft"], ret["fname"], ret["waveform"] = (
            fbank.unsqueeze(1),
            stft.unsqueeze(1),
            fname,
            waveform.unsqueeze(1),
        )

        return ret

    # def time_shuffle_operation(self, fbank):
    #     if(self.time_shuffle == 1):
    #         return fbank

    #     shuffled_fbank = []
    #     for i in range(self.time_shuffle):
    #         shuffled_fbank.append(fbank[:,:, i::self.time_shuffle,:])
    #     return torch.cat(shuffled_fbank, dim=1)

    # def time_unshuffle_operation(self, shuffled_fbank, bs, timesteps, fbins):
    #     if(self.time_shuffle == 1):
    #         return shuffled_fbank

    #     buffer = torch.zeros((bs, 1, timesteps, fbins)).to(shuffled_fbank.device)
    #     for i in range(self.time_shuffle):
    #         buffer[:,0,i::self.time_shuffle,:] = shuffled_fbank[:,i,:,:]
    #     return buffer

    def freq_split_subband(self, fbank):
        if self.subband == 1 or self.image_key != "stft":
            return fbank

        bs, ch, tstep, fbins = fbank.size()

        assert fbank.size(-1) % self.subband == 0
        assert ch == 1

        return (
            fbank.squeeze(1)
            .reshape(bs, tstep, self.subband, fbins // self.subband)
            .permute(0, 2, 1, 3)
        )

    def freq_merge_subband(self, subband_fbank):
        if self.subband == 1 or self.image_key != "stft":
            return subband_fbank
        assert subband_fbank.size(1) == self.subband  # Channel dimension
        bs, sub_ch, tstep, fbins = subband_fbank.size()
        return subband_fbank.permute(0, 2, 1, 3).reshape(bs, tstep, -1).unsqueeze(1)

    def training_step(self, batch, batch_idx):
        g_opt, d_opt = self.optimizers()
        inputs_dict = self.get_input(batch)
        inputs = inputs_dict[self.image_key]
        waveform = inputs_dict["waveform"]

        if batch_idx % 5000 == 0 and self.local_rank == 0:
            print("Log train image")
            self.log_images(inputs, waveform=waveform)

        reconstructions, posterior = self(inputs)

        if self.image_key == "stft":
            rec_waveform = self.decode_to_waveform(reconstructions)
        else:
            rec_waveform = None

        # train the discriminator
        # If working on waveform, inputs is STFT, reconstructions are the waveform
        # If working on the melspec, inputs is melspec, reconstruction are also mel spec
        discloss, log_dict_disc = self.loss(
            inputs=inputs,
            reconstructions=reconstructions,
            posteriors=posterior,
            waveform=waveform,
            rec_waveform=rec_waveform,
            optimizer_idx=1,
            global_step=self.global_step,
            last_layer=self.get_last_layer(),
            split="train",
        )

        self.log(
            "discloss",
            discloss,
            prog_bar=True,
            logger=True,
            on_step=True,
            on_epoch=True,
        )
        self.log_dict(
            log_dict_disc, prog_bar=False, logger=True, on_step=True, on_epoch=False
        )
        d_opt.zero_grad()
        self.manual_backward(discloss)
        d_opt.step()

        self.log(
            "train_step",
            self.train_step,
            prog_bar=False,
            logger=False,
            on_step=True,
            on_epoch=False,
        )

        self.log(
            "global_step",
            float(self.global_step),
            prog_bar=True,
            logger=True,
            on_step=True,
            on_epoch=False,
        )

        aeloss, log_dict_ae = self.loss(
            inputs=inputs,
            reconstructions=reconstructions,
            posteriors=posterior,
            waveform=waveform,
            rec_waveform=rec_waveform,
            optimizer_idx=0,
            global_step=self.global_step,
            last_layer=self.get_last_layer(),
            split="train",
        )
        self.log(
            "aeloss",
            aeloss,
            prog_bar=True,
            logger=True,
            on_step=True,
            on_epoch=False,
        )
        self.log(
            "posterior_std",
            torch.mean(posterior.var),
            prog_bar=True,
            logger=True,
            on_step=True,
            on_epoch=False,
        )
        self.log_dict(
            log_dict_ae, prog_bar=True, logger=True, on_step=True, on_epoch=False
        )

        self.train_step += 1
        g_opt.zero_grad()
        self.manual_backward(aeloss)
        g_opt.step()

    def validation_step(self, batch, batch_idx):
        inputs_dict = self.get_input(batch)
        inputs = inputs_dict[self.image_key]
        waveform = inputs_dict["waveform"]

        if batch_idx <= 3:
            print("Log val image")
            self.log_images(inputs, train=False, waveform=waveform)

        reconstructions, posterior = self(inputs)

        if self.image_key == "stft":
            rec_waveform = self.decode_to_waveform(reconstructions)
        else:
            rec_waveform = None

        aeloss, log_dict_ae = self.loss(
            inputs=inputs,
            reconstructions=reconstructions,
            posteriors=posterior,
            waveform=waveform,
            rec_waveform=rec_waveform,
            optimizer_idx=0,
            global_step=self.global_step,
            last_layer=self.get_last_layer(),
            split="val",
        )

        discloss, log_dict_disc = self.loss(
            inputs=inputs,
            reconstructions=reconstructions,
            posteriors=posterior,
            waveform=waveform,
            rec_waveform=rec_waveform,
            optimizer_idx=1,
            global_step=self.global_step,
            last_layer=self.get_last_layer(),
            split="val",
        )

        self.log_dict(log_dict_ae)
        self.log_dict(log_dict_disc)
        return self.log_dict

    def test_step(self, batch, batch_idx):
        inputs_dict = self.get_input(batch)
        inputs = inputs_dict[self.image_key]
        waveform = inputs_dict["waveform"]
        fnames = inputs_dict["fname"]

        reconstructions, posterior = self(inputs)
        save_path = os.path.join(
            self.get_log_dir(), "autoencoder_result_audiocaps", str(self.global_step)
        )

        if self.image_key == "stft":
            wav_prediction = self.decode_to_waveform(reconstructions)
            wav_original = waveform
            self.save_wave(
                wav_prediction, fnames, os.path.join(save_path, "stft_wav_prediction")
            )
        else:
            wav_vocoder_gt, wav_prediction = synth_one_sample(
                inputs.squeeze(1),
                reconstructions.squeeze(1),
                labels="validation",
                vocoder=self.vocoder,
            )
            self.save_wave(
                wav_vocoder_gt, fnames, os.path.join(save_path, "fbank_vocoder_gt_wave")
            )
            self.save_wave(
                wav_prediction, fnames, os.path.join(save_path, "fbank_wav_prediction")
            )

    def save_wave(self, batch_wav, fname, save_dir):
        os.makedirs(save_dir, exist_ok=True)

        for wav, name in zip(batch_wav, fname):
            name = os.path.basename(name)

            sf.write(os.path.join(save_dir, name), wav, samplerate=self.sampling_rate)

    def configure_optimizers(self):
        lr = self.learning_rate
        params = (
            list(self.encoder.parameters())
            + list(self.decoder.parameters())
            + list(self.quant_conv.parameters())
            + list(self.post_quant_conv.parameters())
        )

        if self.image_key == "stft":
            params += list(self.wave_decoder.parameters())

        opt_ae = torch.optim.Adam(params, lr=lr, betas=(0.5, 0.9))

        if self.image_key == "fbank":
            disc_params = self.loss.discriminator.parameters()
        elif self.image_key == "stft":
            disc_params = itertools.chain(
                self.loss.msd.parameters(), self.loss.mpd.parameters()
            )

        opt_disc = torch.optim.Adam(disc_params, lr=lr, betas=(0.5, 0.9))
        return [opt_ae, opt_disc], []

    def get_last_layer(self):
        return self.decoder.conv_out.weight

    @torch.no_grad()
    def log_images(self, batch, train=True, only_inputs=False, waveform=None, **kwargs):
        log = dict()
        x = batch.to(self.device)
        if not only_inputs:
            xrec, posterior = self(x)
            log["samples"] = self.decode(posterior.sample())
            log["reconstructions"] = xrec

        log["inputs"] = x
        wavs = self._log_img(log, train=train, index=0, waveform=waveform)
        return wavs

    def _log_img(self, log, train=True, index=0, waveform=None):
        images_input = self.tensor2numpy(log["inputs"][index, 0]).T
        images_reconstruct = self.tensor2numpy(log["reconstructions"][index, 0]).T
        images_samples = self.tensor2numpy(log["samples"][index, 0]).T

        if train:
            name = "train"
        else:
            name = "val"

        if self.logger is not None:
            self.logger.log_image(
                "img_%s" % name,
                [images_input, images_reconstruct, images_samples],
                caption=["input", "reconstruct", "samples"],
            )

        inputs, reconstructions, samples = (
            log["inputs"],
            log["reconstructions"],
            log["samples"],
        )

        if self.image_key == "fbank":
            wav_original, wav_prediction = synth_one_sample(
                inputs[index],
                reconstructions[index],
                labels="validation",
                vocoder=self.vocoder,
            )
            wav_original, wav_samples = synth_one_sample(
                inputs[index], samples[index], labels="validation", vocoder=self.vocoder
            )
            wav_original, wav_samples, wav_prediction = (
                wav_original[0],
                wav_samples[0],
                wav_prediction[0],
            )
        elif self.image_key == "stft":
            wav_prediction = (
                self.decode_to_waveform(reconstructions)[index, 0]
                .cpu()
                .detach()
                .numpy()
            )
            wav_samples = (
                self.decode_to_waveform(samples)[index, 0].cpu().detach().numpy()
            )
            wav_original = waveform[index, 0].cpu().detach().numpy()

        if self.logger is not None:
            self.logger.experiment.log(
                {
                    "original_%s"
                    % name: wandb.Audio(
                        wav_original, caption="original", sample_rate=self.sampling_rate
                    ),
                    "reconstruct_%s"
                    % name: wandb.Audio(
                        wav_prediction,
                        caption="reconstruct",
                        sample_rate=self.sampling_rate,
                    ),
                    "samples_%s"
                    % name: wandb.Audio(
                        wav_samples, caption="samples", sample_rate=self.sampling_rate
                    ),
                }
            )

        return wav_original, wav_prediction, wav_samples

    def tensor2numpy(self, tensor):
        return tensor.cpu().detach().numpy()

    def to_rgb(self, x):
        assert self.image_key == "segmentation"
        if not hasattr(self, "colorize"):
            self.register_buffer("colorize", torch.randn(3, x.shape[1], 1, 1).to(x))
        x = F.conv2d(x, weight=self.colorize)
        x = 2.0 * (x - x.min()) / (x.max() - x.min()) - 1.0
        return x


class IdentityFirstStage(torch.nn.Module):
    def __init__(self, *args, vq_interface=False, **kwargs):
        self.vq_interface = vq_interface  # TODO: Should be true by default but check to not break older stuff
        super().__init__()

    def encode(self, x, *args, **kwargs):
        return x

    def decode(self, x, *args, **kwargs):
        return x

    def quantize(self, x, *args, **kwargs):
        if self.vq_interface:
            return x, None, [None, None, None]
        return x

    def forward(self, x, *args, **kwargs):
        return x
