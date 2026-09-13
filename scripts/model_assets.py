"""Verify reviewed public model bytes before starting an ONNX session."""
import hashlib
import json
import importlib.metadata
import shutil
import tempfile
from pathlib import Path


def verify_assets(directory, expected):
    assets=[]
    for name,wanted in sorted(expected.items()):
        path=Path(directory)/name
        digest=hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda:stream.read(1024*1024),b""):
                digest.update(block)
        if digest.hexdigest()!=wanted:
            raise ValueError("Model asset integrity failure")
        assets.append({"name":name,"sha256":wanted})
    return assets


def reviewed_model(cache, name, download=False):
    lock=json.loads(Path(__file__).with_name("model-assets-lock.json").read_text(encoding="utf-8"))
    spec=lock["models"][name]
    if "derived_from" in spec:
        expected=dict(lock["models"][spec["derived_from"]]["assets"],**spec["asset_overrides"])
        identity=hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()
        directory=Path(cache)/"derived"/identity
        if directory.exists():
            return directory,verify_assets(directory,expected)
        if not download:
            raise FileNotFoundError("Reviewed derived model missing; run download-reranker during setup")
        from filelock import FileLock
        directory.parent.mkdir(parents=True,exist_ok=True)
        with FileLock(str(directory)+".lock",timeout=120):
            if directory.exists():
                return directory,verify_assets(directory,expected)
            source,_=reviewed_model(cache,spec["derived_from"],download=True)
            with tempfile.TemporaryDirectory(prefix="model-build-",dir=directory.parent) as temporary:
                stage=Path(temporary)/"ready"
                stage.mkdir()
                for asset in expected:
                    destination=stage/asset
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    if asset in spec["asset_overrides"]:
                        derive_range7(source/asset,destination)
                    else:
                        shutil.copyfile(source/asset,destination)
                assets=verify_assets(stage,expected)
                stage.rename(directory)
                return directory,assets
    from huggingface_hub import snapshot_download
    directory=snapshot_download(repo_id=spec["repository"],revision=spec["revision"],
        cache_dir=str(cache),allow_patterns=list(spec["assets"]),local_files_only=not download,token=False)
    return Path(directory),verify_assets(directory,spec["assets"])


def derive_range7(source,destination):
    # Output checksum approval remains mandatory even with matching build tools.
    for package,version in {"onnxruntime":"1.29.0","onnx":"1.19.1"}.items():
        if importlib.metadata.version(package)!=version:
            raise ValueError("Model build requires pinned model-build-requirements.txt")
    from onnxruntime.quantization import QuantType, quantize_dynamic
    quantize_dynamic(str(source),str(destination),weight_type=QuantType.QInt8,
                     per_channel=True,reduce_range=True,op_types_to_quantize=["MatMul","Gemm"])
