from collections import OrderedDict
import io
import json
from pathlib import Path
import pickle
import struct
import tempfile
import unittest
import zipfile

import audit_retrieval_training_lineage as lineage


class StorageMarker:
    pass


class TensorProxy:
    def __reduce__(self):
        return lineage.rebuild_tensor, (StorageMarker(), 0, (1,), (1,), False, OrderedDict())


class DescriptorPickler(pickle.Pickler):
    def persistent_id(self, obj):
        if isinstance(obj, StorageMarker):
            return ("storage", "float32", "0", "cpu", 1)


class LineageTests(unittest.TestCase):
    def test_overlap_requires_same_ids_and_checks_query_text(self):
        dataset = {"q1": {"question": "One?", "question_type": "multi", "session_ids": ["shared"]},
                   "q2_abs": {"question": "Two?", "question_type": "single", "session_ids": ["shared"]}}
        training = [{"question_id": "q1", "query": "One?", "request_path": "synthesize_oracle", "candidates": [{"grade": 2}]}]
        result = lineage.overlap(training, dataset)
        self.assertEqual(result["overlap_question_ids"], 1)
        self.assertEqual(result["exact_query_text_matches_among_overlap"], 1)
        self.assertEqual(result["unmatched_abstention_id_count"], 1)
        self.assertEqual(result["shared_source_session_ids_across_matched_unmatched_evaluation_histories"], 1)
        training[0]["query"] = "Changed?"
        self.assertEqual(lineage.overlap(training, dataset)["exact_query_text_matches_among_overlap"], 0)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            lineage.overlap(training * 2, dataset)
        self.assertNotIn("One?", json.dumps(result))

    def test_descriptor_reader_rejects_arbitrary_global(self):
        with self.assertRaisesRegex(ValueError, "unsupported checkpoint global"):
            lineage.DescriptorUnpickler(io.BytesIO(b"cos\nsystem\n(S'false'\ntR.")).load()

    def test_narrow_checkpoint_reader_without_torch(self):
        stream = io.BytesIO()
        DescriptorPickler(stream, protocol=2).dump({"state_dict": OrderedDict([("sample", TensorProxy())]), "epoch": 47})
        data = stream.getvalue().replace(b"caudit_retrieval_training_lineage\nrebuild_tensor\n", b"ctorch._utils\n_rebuild_tensor_v2\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("model/data.pkl", data)
                archive.writestr("model/byteorder", "little")
                archive.writestr("model/data/0", struct.pack("<f", 1.25))
            tensors, meta, keys = lineage.checkpoint_tensors(path)
            self.assertEqual(tensors["sample"], ((1,), struct.pack("<f", 1.25)))
            self.assertEqual(meta["epoch"], 47)
            self.assertEqual(keys, ["epoch"])

    def test_export_preserves_order_bytes_and_sentinel_name(self):
        tensors = {name: ((1,), struct.pack("<f", 0.25)) for name in lineage.EXPORT_NAMES[:-1]}
        tensors["output_scale"] = ((1,), struct.pack("<f", 1.0))
        raw = lineage.export_bytes(tensors)
        self.assertEqual(struct.unpack("<H", raw[:2])[0], 15)
        self.assertIn(b"output_scale.weight", raw)
        self.assertEqual(raw[-4:], struct.pack("<f", 1.0))
        tensors.pop("embed_fc1.bias")
        with self.assertRaisesRegex(ValueError, "names differ"):
            lineage.export_bytes(tensors)

    def test_unsupported_storage_or_noncontiguous_tensor_fails(self):
        reader = lineage.DescriptorUnpickler(io.BytesIO())
        with self.assertRaisesRegex(ValueError, "storage type"):
            reader.persistent_load(("storage", "float64", "0", "cpu", 2))
        with self.assertRaisesRegex(ValueError, "contiguous"):
            lineage.rebuild_tensor(lineage.Storage("0", 10), 0, (2,), (2,), False, {})
        with self.assertRaisesRegex(ValueError, "exceeds storage"):
            lineage.rebuild_tensor(lineage.Storage("0", 1), 0, (2,), (1,), False, {})

    def test_split_is_default_not_claimed_historical_partition(self):
        source = "def split(rows, val_frac=0.1, seed=1234):\n    pass\n"
        result = lineage.default_split(source, 474)
        self.assertEqual((result["training_rows"], result["validation_rows"]), (427, 47))
        self.assertFalse(result["historical_partition_proven"])
        with self.assertRaisesRegex(ValueError, "defaults changed"):
            lineage.default_split(source.replace("0.1", "0.2"), 474)


if __name__ == "__main__":
    unittest.main()
