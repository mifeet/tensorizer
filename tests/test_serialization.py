import gc
import os
import tempfile
import unittest
import re
from typing import Tuple

import torch

from transformers import AutoModelForCausalLM

from tensorizer.serialization import TensorSerializer, TensorDeserializer
from tensorizer import utils

model_name = "EleutherAI/gpt-neo-125M"


def serialize_model(model_name: str, device: str) -> Tuple[str, dict]:
    model = AutoModelForCausalLM.from_pretrained(model_name).to(device)
    sd = model.state_dict()
    out_file = tempfile.NamedTemporaryFile("wb+", delete=False)
    try:
        serializer = TensorSerializer(out_file)
        serializer.write_state_dict(sd)
        serializer.close()
    except Exception:
        os.unlink(out_file)
        raise
    return out_file.name, sd


def check_deserialized(deserialized, model_name: str):
    orig_sd = AutoModelForCausalLM.from_pretrained(model_name).state_dict()
    for k, v in deserialized.items():
        assert k in orig_sd
        assert v.size() == orig_sd[k].size()
        assert v.dtype == orig_sd[k].dtype
        assert torch.all(orig_sd[k].to(v.device) == v)
    del orig_sd
    gc.collect()


class TestSerialization(unittest.TestCase):
    def test_serialization(self):
        for device in "cuda", "cpu":
            with self.subTest(msg=f"Serializing with device {device}"):
                gc.collect()
                before_serialization = utils.get_mem_usage()
                serialized_model, orig_sd = serialize_model(model_name, device)
                after_serialization = utils.get_mem_usage()
                print(f"Before serialization: {before_serialization}")
                print(f"After serialization:  {after_serialization}")
                del orig_sd
                try:
                    with open(serialized_model, "rb") as in_file:
                        deserialized = TensorDeserializer(
                            in_file, device="cpu"
                        )
                        check_deserialized(deserialized, model_name)
                        deserialized.close()
                        del deserialized
                finally:
                    os.unlink(serialized_model)


class TestDeserialization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serialized_model_path, sd = serialize_model(model_name, "cpu")
        del sd
        cls._serialized_model_path = serialized_model_path
        gc.collect()

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls._serialized_model_path)

    def test_default_cpu(self):
        in_file = open(self._serialized_model_path, "rb")
        gc.collect()
        before_deserialization = utils.get_mem_usage()
        deserialized = TensorDeserializer(in_file,
                                          device="cpu")
        after_deserialization = utils.get_mem_usage()
        check_deserialized(deserialized, model_name)
        deserialized.close()
        print(f"Before deserialization: {before_deserialization}")
        print(f"After deserialization:  {after_deserialization}")

    def test_default_gpu(self):
        in_file = open(self._serialized_model_path, "rb")
        gc.collect()
        before_deserialization = utils.get_mem_usage()
        deserialized = TensorDeserializer(in_file,
                                          device="cuda")
        check_deserialized(deserialized, model_name)
        after_deserialization = utils.get_mem_usage()
        deserialized.close()
        print(f"Before deserialization: {before_deserialization}")
        print(f"After deserialization:  {after_deserialization}")
        del in_file
        gc.collect()
        after_del = utils.get_mem_usage()
        print(f"After del: {after_del}")

    def test_on_demand(self):
        in_file = open(self._serialized_model_path, "rb")
        deserialized = TensorDeserializer(in_file,
                                          device="cpu",
                                          on_demand=True)

        check_deserialized(deserialized, model_name)
        deserialized.close()

    def test_plaid_mode(self):
        in_file = open(self._serialized_model_path, "rb")
        deserialized = TensorDeserializer(in_file,
                                          device="cuda",
                                          plaid_mode=True)

        check_deserialized(deserialized, model_name)
        deserialized.close()

    def test_plaid_mode_guards(self):
        in_file = open(self._serialized_model_path, "rb")
        deserialized = TensorDeserializer(in_file,
                                          device="cuda",
                                          plaid_mode=True)
        keys = list(deserialized.keys())
        _ = deserialized[keys[0]]
        _ = deserialized[keys[1]]

        with self.assertRaises(RuntimeError):
            _ = deserialized[keys[0]]

        deserialized.close()

    def test_filter_func(self):
        # These two filters should produce identical results
        pattern = re.compile(r"transformer\.h\.0.*")

        def custom_check(tensor_name: str) -> bool:
            return tensor_name.startswith("transformer.h.0")

        with self.subTest(msg="Testing no filter_func"):
            in_file = open(self._serialized_model_path, "rb")
            deserialized = TensorDeserializer(in_file,
                                              device="cuda",
                                              filter_func=None)
            all_keys = set(deserialized.keys())
            assert all_keys, "Deserializing the model with no filter_func" \
                             " loaded an empty set of tensors"
            check_deserialized(deserialized, model_name)
            deserialized.close()

        expected_regex_keys = set(filter(pattern.match, all_keys))
        expected_custom_keys = set(filter(custom_check, all_keys))

        assert (
            expected_regex_keys and expected_regex_keys < all_keys
            and expected_custom_keys and expected_custom_keys < all_keys
        ), "The filter_func test cannot continue" \
           " because a filter_func used in the test" \
           " does not appear in the test model," \
           " or matches all tensor names." \
           " Update the pattern and/or custom_check" \
           " to use more informative filtering criteria." \
           "\n\nTensors present in the model: " + " ".join(all_keys)

        with self.subTest(msg="Testing regex filter_func"):
            in_file = open(self._serialized_model_path, "rb")
            deserialized = TensorDeserializer(in_file,
                                              device="cuda",
                                              filter_func=pattern.match)
            regex_keys = set(deserialized.keys())
            # Test that the deserialized tensors form a proper,
            # non-empty subset of the original list of tensors.
            assert regex_keys == expected_regex_keys
            check_deserialized(deserialized, model_name)
            deserialized.close()

        with self.subTest(msg="Testing custom filter_func"):
            in_file = open(self._serialized_model_path, "rb")
            deserialized = TensorDeserializer(in_file,
                                              device="cuda",
                                              filter_func=custom_check)
            custom_keys = set(deserialized.keys())
            assert custom_keys == expected_custom_keys
            check_deserialized(deserialized, model_name)
            deserialized.close()
