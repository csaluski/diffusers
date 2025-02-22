import inspect
import warnings
import random
import sys
from typing import List, Optional, Union

import torch
import numpy as np

from tqdm.auto import tqdm

from transformers import CLIPFeatureExtractor, CLIPTextModel, CLIPTokenizer

from ...models import AutoencoderKL, UNet2DConditionModel
from ...pipeline_utils import DiffusionPipeline
from ...schedulers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
from . import StableDiffusionPipelineOutput
from .safety_checker import StableDiffusionSafetyChecker


class StableDiffusionPipeline(DiffusionPipeline):
    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: Union[DDIMScheduler, PNDMScheduler, LMSDiscreteScheduler],
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPFeatureExtractor,
    ):
        super().__init__()
        scheduler = scheduler.set_format("pt")
        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
        )

    @torch.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        latents: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        **kwargs,
    ):
        if "torch_device" in kwargs:
            device = kwargs.pop("torch_device")
            warnings.warn(
                "`torch_device` is deprecated as an input argument to `__call__` and will be removed in v0.3.0."
                " Consider using `pipe.to(torch_device)` instead."
            )

            # Set device as before (to be removed in 0.3.0)
            if device is None:
                device = "cuda" if torch.cuda.is_available() else "cpu"
            self.to(device)

        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        # get prompt text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            max_length = text_input.input_ids.shape[-1]
            uncond_input = self.tokenizer(
                [""] * batch_size, padding="max_length", max_length=max_length, return_tensors="pt"
            )
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        # get the initial random noise unless the user supplied it
        latents_shape = (batch_size, self.unet.in_channels, height // 8, width // 8)
        if latents is None:
            latents = torch.randn(
                latents_shape,
                generator=generator,
                device=self.device,
            )
        else:
            if latents.shape != latents_shape:
                raise ValueError(f"Unexpected latents shape, got {latents.shape}, expected {latents_shape}")
            latents = latents.to(self.device)

        # set timesteps
        accepts_offset = "offset" in set(inspect.signature(self.scheduler.set_timesteps).parameters.keys())
        extra_set_kwargs = {}
        if accepts_offset:
            extra_set_kwargs["offset"] = 1

        self.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)

        # if we use LMSDiscreteScheduler, let's make sure latents are mulitplied by sigmas
        if isinstance(self.scheduler, LMSDiscreteScheduler):
            latents = latents * self.scheduler.sigmas[0]

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        for i, t in tqdm(enumerate(self.scheduler.timesteps)):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                sigma = self.scheduler.sigmas[i]
                # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
                latent_model_input = latent_model_input / ((sigma**2 + 1) ** 0.5)

            # predict the noise residual
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                latents = self.scheduler.step(noise_pred, i, latents, **extra_step_kwargs).prev_sample
            else:
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()

        # run safety checker
        #safety_cheker_input = self.feature_extractor(self.numpy_to_pil(image), return_tensors="pt").to(self.device)
        #image, has_nsfw_concept = self.safety_checker(images=image, clip_input=safety_cheker_input.pixel_values)
        has_nsfw_concept = False
        
        if output_type == "pil":
            image = self.numpy_to_pil(image)

        return {"sample": image, "nsfw_content_detected": has_nsfw_concept}

    def get_text_latent_space(self, prompt, guidance_scale = 7.5):

        # get prompt text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]
        
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            max_length = text_input.input_ids.shape[-1]
            uncond_input = self.tokenizer(
                [""], padding="max_length", max_length=max_length, return_tensors="pt"
            )
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        return text_embeddings
    
    def slerp(self, t, v0, v1, DOT_THRESHOLD=0.9995):
        """ helper function to spherically interpolate two arrays v1 v2 
        from https://gist.github.com/karpathy/00103b0037c5aaea32fe1da1af553355 
        this should be better than lerping for moving between noise spaces """

        if not isinstance(v0, np.ndarray):
            inputs_are_torch = True
            input_device = v0.device
            v0 = v0.cpu().numpy()
            v1 = v1.cpu().numpy()

        dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
        if np.abs(dot) > DOT_THRESHOLD:
            v2 = (1 - t) * v0 + t * v1
        else:
            theta_0 = np.arccos(dot)
            sin_theta_0 = np.sin(theta_0)
            theta_t = theta_0 * t
            sin_theta_t = np.sin(theta_t)
            s0 = np.sin(theta_0 - theta_t) / sin_theta_0
            s1 = sin_theta_t / sin_theta_0
            v2 = s0 * v0 + s1 * v1

        if inputs_are_torch:
            v2 = torch.from_numpy(v2).to(input_device)

        return v2

    def lerp_between_prompts(self, first_prompt, second_prompt, seed = None, length = 10, save=False, guidance_scale: Optional[float] = 7.5, **kwargs):
        first_embedding = self.get_text_latent_space(first_prompt)
        second_embedding = self.get_text_latent_space(second_prompt)
        if not seed:
            seed = random.randint(0, sys.maxsize)
        generator = torch.Generator(self.device)
        generator.manual_seed(seed)
        generator_state = generator.get_state()
        lerp_embed_points = []
        for i in range(length):
            weight = i / length
            tensor_lerp = torch.lerp(first_embedding, second_embedding, weight)
            lerp_embed_points.append(tensor_lerp)
        images = []
        for idx, latent_point in enumerate(lerp_embed_points):
            generator.set_state(generator_state)
            image = self.diffuse_from_inits(latent_point, **kwargs)["image"][0]
            images.append(image)
            if save:
                image.save(f"{first_prompt}-{second_prompt}-{idx:02d}.png", "PNG")
        return {"images": images, "latent_points": lerp_embed_points,"generator_state": generator_state}

    def slerp_through_seeds(self,
        prompt,
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        save = False,
        seed = None, steps = 10, **kwargs):

        if not seed:
            seed = random.randint(0, sys.maxsize)
        generator = torch.Generator(self.device)
        generator.manual_seed(seed)
        init_start = torch.randn(
            (1, self.unet.in_channels, height // 8, width // 8), 
            generator = generator, device = self.device)
        init_end = torch.randn(
            (1, self.unet.in_channels, height // 8, width // 8), 
            generator = generator, device = self.device)
        generator_state = generator.get_state()
        slerp_embed_points = []
        # weight from 0 to 1/(steps - 1), add init_end specifically so that we 
        # have len(images) = steps
        for i in range(steps - 1):
            weight = i / steps
            tensor_slerp = self.slerp(weight, init_start, init_end)
            slerp_embed_points.append(tensor_slerp)
        slerp_embed_points.append(init_end)
        images = []
        embed_point = self.get_text_latent_space(prompt)
        for idx, noise_point in enumerate(slerp_embed_points):
            generator.set_state(generator_state)
            image = self.diffuse_from_inits(embed_point, init = noise_point, **kwargs)["image"][0]
            images.append(image)
            if save:
                image.save(f"{seed}-{idx:02d}.png", "PNG")
        return {"images": images, "noise_samples": slerp_embed_points,"generator_state": generator_state}

    @torch.no_grad()
    def diffuse_from_inits(self, text_embeddings, 
        init = None,
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        output_type: Optional[str] = "pil",
        save_n_steps: Optional[int] = None,
        **kwargs,):

        batch_size = 1
        
        if generator == None:
            generator = torch.Generator("cuda")
        generator_state = generator.get_state()
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get the intial random noise
        latents = init if init is not None else torch.randn(
            (batch_size, self.unet.in_channels, height // 8, width // 8),
            generator=generator,
            device=self.device,)

        # set timesteps
        accepts_offset = "offset" in set(inspect.signature(self.scheduler.set_timesteps).parameters.keys())
        extra_set_kwargs = {}
        if accepts_offset:
            extra_set_kwargs["offset"] = 1

        self.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)

        # if we use LMSDiscreteScheduler, let's make sure latents are mulitplied by sigmas
        if isinstance(self.scheduler, LMSDiscreteScheduler):
            latents = latents * self.scheduler.sigmas[0]

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta
        if save_n_steps:
            mid_latents = []
            mid_images = []
        else: 
            mid_latents = None
            mid_images = None
        for i, t in tqdm(enumerate(self.scheduler.timesteps)):
            if save_n_steps:
                if i % save_n_steps == 0:
                    # scale and decode the image latents with vae
                    dec_mid_latents = 1 / 0.18215 * latents
                    mid_latents.append(dec_mid_latents)
                    image = self.vae.decode(dec_mid_latents).sample

                    image = (image / 2 + 0.5).clamp(0, 1)
                    image = image.cpu().permute(0, 2, 3, 1).numpy()
                    
                    if output_type == "pil":
                        image = self.numpy_to_pil(image)
                    mid_latents.append(image)
                    mid_images.append(image)
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                sigma = self.scheduler.sigmas[i]
                latent_model_input = latent_model_input / ((sigma**2 + 1) ** 0.5)

            # predict the noise residual
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings)["sample"]

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                latents = self.scheduler.step(noise_pred, i, latents, **extra_step_kwargs)["prev_sample"]
            else:
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs)["prev_sample"]

        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()
        
        if output_type == "pil":
            image = self.numpy_to_pil(image)

        return {"image": image, "generator_state": generator_state, "mid_latents": mid_latents, "mid_images": mid_images}

    def variation(self, text_embeddings, generator_state, variation_magnitude = 100, **kwargs):
        # random vector to move in latent space
        rand_t = (torch.rand(text_embeddings.shape, device = self.device) * 2) - 1
        rand_mag = torch.sum(torch.abs(rand_t)) / variation_magnitude
        scaled_rand_t = rand_t / rand_mag
        variation_embedding = text_embeddings + scaled_rand_t
        
        generator = torch.Generator("cuda")
        generator.set_state(generator_state)
        result = self.diffuse_from_inits(variation_embedding, generator=generator, **kwargs)
        result.update({"latent_point": variation_embedding})
        return result
