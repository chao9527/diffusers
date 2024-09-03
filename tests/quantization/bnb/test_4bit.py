# coding=utf-8
# Copyright 2024 The HuggingFace Team Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a clone of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import gc
import tempfile
import unittest

import numpy as np

from diffusers import BitsAndBytesConfig, DiffusionPipeline, FluxTransformer2DModel, SD3Transformer2DModel
from diffusers.utils.testing_utils import (
    is_bitsandbytes_available,
    is_torch_available,
    is_transformers_available,
    load_pt,
    require_accelerate,
    require_bitsandbytes_version_greater,
    require_torch,
    require_torch_gpu,
    require_transformers_version_greater,
    slow,
    torch_device,
)


def get_some_linear_layer(model):
    if model.__class__.__name__ == "SD3Transformer2DModel":
        return model.transformer_blocks[0].attn.to_q
    else:
        return NotImplementedError("Don't know what layer to retrieve here.")


if is_transformers_available():
    from transformers import T5EncoderModel

if is_torch_available():
    import torch


if is_bitsandbytes_available():
    import bitsandbytes as bnb


@require_bitsandbytes_version_greater("0.43.2")
@require_accelerate
@require_torch
@require_torch_gpu
@slow
class Base4bitTests(unittest.TestCase):
    # We need to test on relatively large models (aka >1b parameters otherwise the quantiztion may not work as expected)
    # Therefore here we use only SD3 to test our module
    model_name = "stabilityai/stable-diffusion-3-medium-diffusers"

    # This was obtained on audace so the number might slightly change
    expected_rel_difference = 3.69

    prompt = "a beautiful sunset amidst the mountains."
    num_inference_steps = 10
    seed = 0

    def get_dummy_inputs(self):
        prompt_embeds = load_pt(
            "https://huggingface.co/datasets/hf-internal-testing/bnb-diffusers-testing-artifacts/resolve/main/prompt_embeds.pt"
        )
        pooled_prompt_embeds = load_pt(
            "https://huggingface.co/datasets/hf-internal-testing/bnb-diffusers-testing-artifacts/resolve/main/pooled_prompt_embeds.pt"
        )
        latent_model_input = load_pt(
            "https://huggingface.co/datasets/hf-internal-testing/bnb-diffusers-testing-artifacts/resolve/main/latent_model_input.pt"
        )

        input_dict_for_transformer = {
            "hidden_states": latent_model_input,
            "encoder_hidden_states": prompt_embeds,
            "pooled_projections": pooled_prompt_embeds,
            "timestep": torch.Tensor([1.0]),
            "return_dict": False,
        }
        return input_dict_for_transformer


class BnB4BitBasicTests(Base4bitTests):
    def setUp(self):
        # Models
        self.model_fp16 = SD3Transformer2DModel.from_pretrained(
            self.model_name, subfolder="transformer", torch_dtype=torch.float16
        )
        nf4_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
        )
        self.model_4bit = SD3Transformer2DModel.from_pretrained(
            self.model_name, subfolder="transformer", quantization_config=nf4_config
        )

    def tearDown(self):
        del self.model_fp16
        del self.model_4bit

        gc.collect()
        torch.cuda.empty_cache()

    def test_quantization_num_parameters(self):
        r"""
        Test if the number of returned parameters is correct
        """
        num_params_4bit = self.model_4bit.num_parameters()
        num_params_fp16 = self.model_fp16.num_parameters()

        self.assertEqual(num_params_4bit, num_params_fp16)

    def test_quantization_config_json_serialization(self):
        r"""
        A simple test to check if the quantization config is correctly serialized and deserialized
        """
        config = self.model_4bit.config

        self.assertTrue("quantization_config" in config)

        _ = config["quantization_config"].to_dict()
        _ = config["quantization_config"].to_diff_dict()

        _ = config["quantization_config"].to_json_string()

    def test_memory_footprint(self):
        r"""
        A simple test to check if the model conversion has been done correctly by checking on the
        memory footprint of the converted model and the class type of the linear layers of the converted models
        """
        from bitsandbytes.nn import Params4bit

        mem_fp16 = self.model_fp16.get_memory_footprint()
        mem_4bit = self.model_4bit.get_memory_footprint()

        self.assertAlmostEqual(mem_fp16 / mem_4bit, self.expected_rel_difference, delta=1e-2)
        linear = get_some_linear_layer(self.model_4bit)
        self.assertTrue(linear.weight.__class__ == Params4bit)

    def test_original_dtype(self):
        r"""
        A simple test to check if the model succesfully stores the original dtype
        """
        self.assertTrue("_pre_quantization_dtype" in self.model_4bit.config)
        self.assertFalse("_pre_quantization_dtype" in self.model_fp16.config)
        self.assertTrue(self.model_4bit.config["_pre_quantization_dtype"] == torch.float16)

    def test_linear_are_4bit(self):
        r"""
        A simple test to check if the model conversion has been done correctly by checking on the
        memory footprint of the converted model and the class type of the linear layers of the converted models
        """
        self.model_fp16.get_memory_footprint()
        self.model_4bit.get_memory_footprint()

        for name, module in self.model_4bit.named_modules():
            if isinstance(module, torch.nn.Linear):
                if name not in self.model_fp16._keep_in_fp32_modules:
                    # 4-bit parameters are packed in uint8 variables
                    self.assertTrue(module.weight.dtype == torch.uint8)

    def test_device_assignment(self):
        mem_before = self.model_4bit.get_memory_footprint()

        # Move to CPU
        self.model_4bit.to("cpu")
        self.assertEqual(self.model_4bit.device.type, "cpu")
        self.assertAlmostEqual(self.model_4bit.get_memory_footprint(), mem_before)

        # Move back to CUDA device
        for device in [0, "cuda", "cuda:0", "call()"]:
            if device == "call()":
                self.model_4bit.cuda(0)
            else:
                self.model_4bit.to(device)
            self.assertEqual(self.model_4bit.device, torch.device(0))
            self.assertAlmostEqual(self.model_4bit.get_memory_footprint(), mem_before)
            self.model_4bit.to("cpu")

    def test_device_and_dtype_assignment(self):
        r"""
        Test whether trying to cast (or assigning a device to) a model after converting it in 4-bit will throw an error.
        Checks also if other models are casted correctly.
        """
        with self.assertRaises(ValueError):
            # Tries with a `dtype`
            self.model_4bit.to(torch.float16)

        with self.assertRaises(ValueError):
            # Tries with a `device` and `dtype`
            self.model_4bit.to(device="cuda:0", dtype=torch.float16)

        with self.assertRaises(ValueError):
            # Tries with a cast
            self.model_4bit.float()

        with self.assertRaises(ValueError):
            # Tries with a cast
            self.model_4bit.half()

        # Test if we did not break anything
        self.model_fp16 = self.model_fp16.to(dtype=torch.float32, device=torch_device)
        input_dict_for_transformer = self.get_dummy_inputs()
        model_inputs = {
            k: v.to(dtype=torch.float32, device=torch_device)
            for k, v in input_dict_for_transformer.items()
            if not isinstance(v, bool)
        }
        model_inputs.update({k: v for k, v in input_dict_for_transformer.items() if k not in model_inputs})
        with torch.no_grad():
            _ = self.model_fp16(**model_inputs)

        # Check this does not throw an error
        _ = self.model_fp16.to("cpu")

        # Check this does not throw an error
        _ = self.model_fp16.half()

        # Check this does not throw an error
        _ = self.model_fp16.float()

        # Check that this does not throw an error
        _ = self.model_fp16.cuda()

    def test_bnb_4bit_wrong_config(self):
        r"""
        Test whether creating a bnb config with unsupported values leads to errors.
        """
        with self.assertRaises(ValueError):
            _ = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_storage="add")


@require_transformers_version_greater("4.44.0")
class SlowBnb4BitTests(Base4bitTests):
    def setUp(self) -> None:
        nf4_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
        )
        model_4bit = SD3Transformer2DModel.from_pretrained(
            self.model_name, subfolder="transformer", quantization_config=nf4_config
        )
        self.pipeline_4bit = DiffusionPipeline.from_pretrained(
            self.model_name, transformer=model_4bit, torch_dtype=torch.float16
        )
        self.pipeline_4bit.enable_model_cpu_offload()

    def tearDown(self):
        del self.pipeline_4bit

        gc.collect()
        torch.cuda.empty_cache()

    def test_quality(self):
        output = self.pipeline_4bit(
            prompt=self.prompt,
            num_inference_steps=self.num_inference_steps,
            generator=torch.manual_seed(self.seed),
            output_type="np",
        ).images

        out_slice = output[0, -3:, -3:, -1].flatten()
        expected_slice = np.array([0.1123, 0.1296, 0.1609, 0.1042, 0.1230, 0.1274, 0.0928, 0.1165, 0.1216])

        self.assertTrue(np.allclose(out_slice, expected_slice, atol=1e-4, rtol=1e-4))

    def test_generate_quality_dequantize(self):
        r"""
        Test that loading the model and unquantize it produce correct results.
        """
        self.pipeline_4bit.transformer.dequantize()
        output = self.pipeline_4bit(
            prompt=self.prompt,
            num_inference_steps=self.num_inference_steps,
            generator=torch.manual_seed(self.seed),
            output_type="np",
        ).images

        out_slice = output[0, -3:, -3:, -1].flatten()
        expected_slice = np.array([0.1216, 0.1387, 0.1584, 0.1152, 0.1318, 0.1282, 0.1062, 0.1226, 0.1228])
        self.assertTrue(np.allclose(out_slice, expected_slice, atol=1e-4, rtol=1e-4))

        # Since we offloaded the `pipeline_4bit.transformer` to CPU (result of `enable_model_cpu_offload()), check
        # the following.
        self.assertTrue(self.pipeline_4bit.transformer.device.type == "cpu")
        # calling it again shouldn't be a problem
        _ = self.pipeline_4bit(
            prompt=self.prompt,
            num_inference_steps=2,
            generator=torch.manual_seed(self.seed),
            output_type="np",
        ).images


@require_transformers_version_greater("4.44.0")
class SlowBnb4BitFluxTests(Base4bitTests):
    def setUp(self) -> None:
        # TODO: Copy sayakpaul/flux.1-dev-nf4-pkg to testing repo.
        model_id = "sayakpaul/flux.1-dev-nf4-pkg"
        t5_4bit = T5EncoderModel.from_pretrained(model_id, subfolder="text_encoder_2")
        transformer_4bit = FluxTransformer2DModel.from_pretrained(model_id, subfolder="transformer")
        self.pipeline_4bit = DiffusionPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-dev",
            text_encoder_2=t5_4bit,
            transformer=transformer_4bit,
            torch_dtype=torch.float16,
        )
        self.pipeline_4bit.enable_model_cpu_offload()

    def tearDown(self):
        del self.pipeline_4bit

        gc.collect()
        torch.cuda.empty_cache()

    def test_quality(self):
        # keep the resolution and max tokens to a lower number for faster execution.
        output = self.pipeline_4bit(
            prompt=self.prompt,
            num_inference_steps=self.num_inference_steps,
            generator=torch.manual_seed(self.seed),
            height=256,
            width=256,
            max_sequence_length=64,
            output_type="np",
        ).images

        out_slice = output[0, -3:, -3:, -1].flatten()
        expected_slice = np.array([0.0583, 0.0586, 0.0632, 0.0815, 0.0813, 0.0947, 0.1040, 0.1145, 0.1265])

        self.assertTrue(np.allclose(out_slice, expected_slice, atol=1e-4, rtol=1e-4))


@slow
class BaseBnb4BitSerializationTests(Base4bitTests):
    def tearDown(self):
        gc.collect()
        torch.cuda.empty_cache()

    def test_serialization(self, quant_type="nf4", double_quant=True, safe_serialization=True):
        r"""
        Test whether it is possible to serialize a model in 4-bit. Uses most typical params as default.
        See ExtendedSerializationTest class for more params combinations.
        """

        self.quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type=quant_type,
            bnb_4bit_use_double_quant=double_quant,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        model_0 = SD3Transformer2DModel.from_pretrained(
            self.model_name, subfolder="transformer", quantization_config=self.quantization_config
        )
        self.assertTrue("_pre_quantization_dtype" in model_0.config)
        with tempfile.TemporaryDirectory() as tmpdirname:
            model_0.save_pretrained(tmpdirname, safe_serialization=safe_serialization)

            config = SD3Transformer2DModel.load_config(tmpdirname)
            self.assertTrue("quantization_config" in config)
            self.assertTrue("_pre_quantization_dtype" not in config)

            model_1 = SD3Transformer2DModel.from_pretrained(tmpdirname)

        # checking quantized linear module weight
        linear = get_some_linear_layer(model_1)
        self.assertTrue(linear.weight.__class__ == bnb.nn.Params4bit)
        self.assertTrue(hasattr(linear.weight, "quant_state"))
        self.assertTrue(linear.weight.quant_state.__class__ == bnb.functional.QuantState)

        # checking memory footpring
        self.assertAlmostEqual(model_0.get_memory_footprint() / model_1.get_memory_footprint(), 1, places=2)

        # Matching all parameters and their quant_state items:
        d0 = dict(model_0.named_parameters())
        d1 = dict(model_1.named_parameters())
        self.assertTrue(d0.keys() == d1.keys())

        for k in d0.keys():
            self.assertTrue(d0[k].shape == d1[k].shape)
            self.assertTrue(d0[k].device.type == d1[k].device.type)
            self.assertTrue(d0[k].device == d1[k].device)
            self.assertTrue(d0[k].dtype == d1[k].dtype)
            self.assertTrue(torch.equal(d0[k], d1[k].to(d0[k].device)))

            if isinstance(d0[k], bnb.nn.modules.Params4bit):
                for v0, v1 in zip(
                    d0[k].quant_state.as_dict().values(),
                    d1[k].quant_state.as_dict().values(),
                ):
                    if isinstance(v0, torch.Tensor):
                        self.assertTrue(torch.equal(v0, v1.to(v0.device)))
                    else:
                        self.assertTrue(v0 == v1)

        # comparing forward() outputs
        dummy_inputs = self.get_dummy_inputs()
        inputs = {k: v.to(torch_device) for k, v in dummy_inputs.items() if isinstance(v, torch.Tensor)}
        inputs.update({k: v for k, v in dummy_inputs.items() if k not in inputs})
        out_0 = model_0(**inputs)[0]
        out_1 = model_1(**inputs)[0]
        self.assertTrue(torch.equal(out_0, out_1))


class ExtendedSerializationTest(BaseBnb4BitSerializationTests):
    """
    tests more combinations of parameters
    """

    def test_nf4_single_unsafe(self):
        self.test_serialization(quant_type="nf4", double_quant=False, safe_serialization=False)

    def test_nf4_single_safe(self):
        self.test_serialization(quant_type="nf4", double_quant=False, safe_serialization=True)

    def test_nf4_double_unsafe(self):
        self.test_serialization(quant_type="nf4", double_quant=True, safe_serialization=False)

    # nf4 double safetensors quantization is tested in test_serialization() method from the parent class

    def test_fp4_single_unsafe(self):
        self.test_serialization(quant_type="fp4", double_quant=False, safe_serialization=False)

    def test_fp4_single_safe(self):
        self.test_serialization(quant_type="fp4", double_quant=False, safe_serialization=True)

    def test_fp4_double_unsafe(self):
        self.test_serialization(quant_type="fp4", double_quant=True, safe_serialization=False)

    def test_fp4_double_safe(self):
        self.test_serialization(quant_type="fp4", double_quant=True, safe_serialization=True)