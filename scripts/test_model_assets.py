"""Asset validation does not trust a new fingerprint as release approval."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch
from scripts import model_assets
from scripts.model_assets import verify_assets
from scripts.vector_pipeline import LocalModel


class AssetTests(unittest.TestCase):
    def derived_fixture(self,root):
        expected={"onnx/model.onnx":hashlib.sha256(b"synthetic-derived").hexdigest()}
        spec={"derived_from":"base","asset_overrides":expected}
        lock={"models":{"derived":spec,"base":{"assets":expected}}}
        identity=hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()
        return lock,Path(root)/"derived"/identity

    def test_portable_derived_cache_loads_without_conversion_or_base(self):
        with tempfile.TemporaryDirectory() as root:
            lock,directory=self.derived_fixture(root)
            (directory/"onnx").mkdir(parents=True)
            (directory/"onnx/model.onnx").write_bytes(b"synthetic-derived")
            with patch.object(model_assets.json,"loads",return_value=lock), patch.object(model_assets,"derive_range7") as build:
                found,assets=model_assets.reviewed_model(root,"derived")
            self.assertEqual(found,directory)
            self.assertEqual(len(assets),1)
            build.assert_not_called()

    def test_missing_derived_cache_requires_explicit_setup(self):
        with tempfile.TemporaryDirectory() as root:
            lock,directory=self.derived_fixture(root)
            with patch.object(model_assets.json,"loads",return_value=lock), patch.object(model_assets,"derive_range7") as build:
                with self.assertRaisesRegex(FileNotFoundError,"setup"):
                    model_assets.reviewed_model(root,"derived")
            build.assert_not_called()
            self.assertFalse(directory.exists())

    def test_tampered_derived_cache_is_not_overwritten_by_setup(self):
        with tempfile.TemporaryDirectory() as root:
            lock,directory=self.derived_fixture(root)
            (directory/"onnx").mkdir(parents=True)
            path=directory/"onnx/model.onnx"
            path.write_bytes(b"tampered-synthetic")
            with patch.object(model_assets.json,"loads",return_value=lock):
                with self.assertRaisesRegex(ValueError,"integrity"):
                    model_assets.reviewed_model(root,"derived",download=True)
            self.assertEqual(path.read_bytes(),b"tampered-synthetic")

    def test_conversion_requires_pinned_build_tools(self):
        with patch.object(model_assets.importlib.metadata,"version",return_value="unapproved"):
            with self.assertRaisesRegex(ValueError,"pinned"):
                model_assets.derive_range7(Path("unused"),Path("unused-output"))

    def test_failed_conversion_does_not_publish_partial_cache(self):
        with tempfile.TemporaryDirectory() as root:
            lock,directory=self.derived_fixture(root)
            resolver=model_assets.reviewed_model
            with patch.object(model_assets.json,"loads",return_value=lock), \
                 patch.object(model_assets,"reviewed_model",return_value=(Path(root),[])), \
                 patch.object(model_assets,"derive_range7",side_effect=RuntimeError("synthetic build failure")):
                with self.assertRaisesRegex(RuntimeError,"synthetic build failure"):
                    resolver(root,"derived",download=True)
            self.assertFalse(directory.exists())
            self.assertFalse(list(directory.parent.glob("model-build-*")))

    def test_built_cache_is_verified_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            lock,directory=self.derived_fixture(root)
            resolver=model_assets.reviewed_model
            def bad_build(source,destination):
                destination.write_bytes(b"wrong synthetic output")
            with patch.object(model_assets.json,"loads",return_value=lock), \
                 patch.object(model_assets,"reviewed_model",return_value=(Path(root),[])), \
                 patch.object(model_assets,"derive_range7",side_effect=bad_build):
                with self.assertRaisesRegex(ValueError,"integrity"):
                    resolver(root,"derived",download=True)
            self.assertFalse(directory.exists())

    def test_unknown_embedding_profile_rejected_before_loading(self):
        with self.assertRaisesRegex(ValueError,"Unreviewed embedding profile"):
            LocalModel(Path("unused-synthetic-cache"),profile="unreviewed")

    def test_integrity_and_missing_file_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"model.onnx"
            path.write_bytes(b"synthetic-reviewed-model")
            expected={"model.onnx":hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertEqual(verify_assets(temp,expected)[0]["name"],"model.onnx")
            path.write_bytes(b"synthetic-tampered-model")
            with self.assertRaisesRegex(ValueError,"integrity"):
                verify_assets(temp,expected)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                verify_assets(temp,expected)
