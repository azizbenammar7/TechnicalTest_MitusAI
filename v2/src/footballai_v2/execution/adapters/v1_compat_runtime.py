"""Local runtime configuration and readiness checks for ``v1_compat``.

This module is intentionally usable without the optional ML dependency set so
normal V2 tests and ``demo_fast`` remain lightweight.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


REPOSITORY_ROOT = Path(
    os.getenv("FOOTBALLAI_APPLICATION_ROOT", str(Path(__file__).resolve().parents[5]))
).expanduser().resolve()
DEFAULT_MODEL_PATH = REPOSITORY_ROOT / ".models" / "yolov8m.pt"
MODEL_NAME = "yolov8m.pt"
SETUP_COMMAND = "make v2-v1-compat-setup"
SUPPORTED_PYTHON_MIN = (3, 11)
SUPPORTED_PYTHON_MAX = (3, 13)
SUPPORTED_PLATFORMS = {"darwin", "linux"}
SUPPORTED_MACHINES = {"arm64", "aarch64", "x86_64", "amd64"}

DEPENDENCIES = (
    ("ultralytics", "ultralytics", "ultralytics"),
    ("LAP assignment solver", "lap", "lap"),
    ("OpenCV", "cv2", "opencv-python-headless"),
    ("PyArrow", "pyarrow", "pyarrow"),
    ("tqdm", "tqdm", "tqdm"),
    ("PyTorch", "torch", "torch"),
)


class V1CompatConfigurationError(ValueError):
    """A safe configuration error that can be exposed to local users."""


@dataclass(frozen=True, slots=True)
class V1CompatConfig:
    target_fps: float
    image_size: int
    confidence: float
    requested_device: str
    selected_device: str
    model_path: Path
    model_sha256: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "target_fps": self.target_fps,
            "image_size": self.image_size,
            "confidence": self.confidence,
            "requested_device": self.requested_device,
            "selected_device": self.selected_device,
            "model_name": MODEL_NAME,
            "model_sha256": self.model_sha256,
        }


@dataclass(frozen=True, slots=True)
class V1CompatReadiness:
    status: str
    missing_requirements: tuple[str, ...]
    runtime_errors: tuple[str, ...]
    runtime: dict[str, Any]
    config: V1CompatConfig | None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def message(self) -> str:
        if self.ready:
            return "V1-compatible analysis is ready."
        return f"V1-compatible analysis is unavailable. Run: {SETUP_COMMAND}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "readiness_status": self.status,
            "available": self.ready,
            "missing_requirements": list(self.missing_requirements),
            "runtime_errors": list(self.runtime_errors),
            "readiness_message": self.message,
            "setup_command": SETUP_COMMAND,
            "runtime": self.runtime,
        }


def configured_model_path(environment: dict[str, str] | None = None) -> Path:
    env = environment if environment is not None else os.environ
    configured = env.get("FOOTBALLAI_V1_COMPAT_MODEL_PATH")
    if not configured:
        return DEFAULT_MODEL_PATH
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_model_file(path: Path) -> tuple[bool, str | None]:
    if not path.is_file():
        return False, "yolov8m.pt weights"
    try:
        if path.name != MODEL_NAME or path.stat().st_size < 1024 * 1024:
            return False, "valid yolov8m.pt weights"
        with path.open("rb") as source:
            if source.read(4) != b"PK\x03\x04":
                return False, "valid yolov8m.pt weights"
    except OSError:
        return False, "readable yolov8m.pt weights"
    return True, None


def resolve_device(torch_module: Any, requested: str) -> str:
    requested = requested.strip().lower()
    if requested not in {"auto", "mps", "cpu", "cuda"}:
        raise V1CompatConfigurationError(
            "FOOTBALLAI_V1_COMPAT_DEVICE must be auto, mps, cpu, or cuda."
        )
    mps_available = bool(
        getattr(getattr(torch_module, "backends", None), "mps", None)
        and torch_module.backends.mps.is_available()
    )
    cuda_available = bool(
        getattr(torch_module, "cuda", None) and torch_module.cuda.is_available()
    )
    if requested == "auto":
        if mps_available:
            return "mps"
        if cuda_available:
            return "cuda"
        return "cpu"
    if requested == "mps" and not mps_available:
        raise V1CompatConfigurationError(
            "Apple MPS was requested but is unavailable. Set FOOTBALLAI_V1_COMPAT_DEVICE=cpu to opt in to CPU execution."
        )
    if requested == "cuda" and not cuda_available:
        raise V1CompatConfigurationError(
            "CUDA was requested but is unavailable. Choose a device explicitly before starting a long analysis."
        )
    return requested


def _number(environment: dict[str, str], name: str, default: str, *, minimum: float, maximum: float) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as exc:
        raise V1CompatConfigurationError(f"{name} must be numeric.") from exc
    if not minimum <= value <= maximum:
        raise V1CompatConfigurationError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return value


def config_from_environment(
    torch_module: Any,
    *,
    environment: dict[str, str] | None = None,
    model_path: Path | None = None,
) -> V1CompatConfig:
    env = dict(os.environ if environment is None else environment)
    target_fps = _number(env, "FOOTBALLAI_V1_COMPAT_TARGET_FPS", "5", minimum=.1, maximum=120)
    image_size_number = _number(env, "FOOTBALLAI_V1_COMPAT_IMAGE_SIZE", "1280", minimum=32, maximum=4096)
    image_size = int(image_size_number)
    if image_size != image_size_number or image_size % 32:
        raise V1CompatConfigurationError("FOOTBALLAI_V1_COMPAT_IMAGE_SIZE must be an integer multiple of 32.")
    confidence = _number(env, "FOOTBALLAI_V1_COMPAT_CONFIDENCE", ".20", minimum=.001, maximum=1)
    selected_device = resolve_device(torch_module, env.get("FOOTBALLAI_V1_COMPAT_DEVICE", "auto"))
    resolved_model = model_path or configured_model_path(env)
    valid, reason = validate_model_file(resolved_model)
    if not valid:
        raise V1CompatConfigurationError(reason or "YOLOv8m weights are invalid.")
    return V1CompatConfig(
        target_fps=target_fps,
        image_size=image_size,
        confidence=confidence,
        requested_device=env.get("FOOTBALLAI_V1_COMPAT_DEVICE", "auto").strip().lower(),
        selected_device=selected_device,
        model_path=resolved_model.resolve(),
        model_sha256=sha256_file(resolved_model),
    )


def _tool_version(name: str) -> str | None:
    try:
        completed = subprocess.run(
            [name, "-version"], shell=False, capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = completed.stdout.splitlines()[0] if completed.stdout else ""
    parts = line.split()
    return parts[2] if len(parts) >= 3 else "available"


def check_v1_compat_readiness(
    *,
    environment: dict[str, str] | None = None,
    python_version: tuple[int, int] | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
    import_module: Callable[[str], Any] = importlib.import_module,
    which: Callable[[str], str | None] = shutil.which,
    model_path: Path | None = None,
) -> V1CompatReadiness:
    env = dict(os.environ if environment is None else environment)
    version = python_version or sys.version_info[:2]
    platform_value = (platform_name or sys.platform).lower()
    machine_value = (machine or platform.machine()).lower()
    missing_packages: list[str] = []
    import_errors: list[str] = []
    modules: dict[str, Any] = {}
    package_versions: dict[str, str] = {}

    for label, module_name, _distribution in DEPENDENCIES:
        try:
            present = find_spec(module_name) is not None
        except (ImportError, ValueError, AttributeError):
            present = False
        if not present:
            missing_packages.append(label)
            continue
        try:
            module = import_module(module_name)
            modules[module_name] = module
            package_versions[label] = str(getattr(module, "__version__", "installed"))
        except Exception:
            import_errors.append(f"{label} could not be imported")

    missing_tools = [name for name in ("ffmpeg", "ffprobe") if which(name) is None]
    resolved_model = model_path or configured_model_path(env)
    model_valid, model_reason = validate_model_file(resolved_model)
    config: V1CompatConfig | None = None
    configuration_errors: list[str] = []
    if "torch" in modules and model_valid:
        try:
            config = config_from_environment(modules["torch"], environment=env, model_path=resolved_model)
        except V1CompatConfigurationError as exc:
            configuration_errors.append(str(exc))

    python_supported = SUPPORTED_PYTHON_MIN <= version <= SUPPORTED_PYTHON_MAX
    platform_supported = platform_value in SUPPORTED_PLATFORMS and machine_value in SUPPORTED_MACHINES
    if not python_supported:
        status = "unsupported_python_version"
    elif not platform_supported:
        status = "unsupported_platform"
    elif missing_packages:
        status = "missing_python_packages"
    elif import_errors or configuration_errors:
        status = "runtime_import_error"
    elif missing_tools:
        status = "missing_system_tools"
    elif not model_valid:
        status = "missing_model_weights"
    else:
        status = "ready"

    missing_requirements = [*missing_packages, *missing_tools]
    if model_reason:
        missing_requirements.append(model_reason)
    runtime_errors = [*import_errors, *configuration_errors]
    runtime: dict[str, Any] = {
        "python_version": f"{version[0]}.{version[1]}",
        "platform": f"{platform_value}/{machine_value}",
        "packages": package_versions,
        "system_tools": {
            "ffmpeg": _tool_version("ffmpeg") if "ffmpeg" not in missing_tools else None,
            "ffprobe": _tool_version("ffprobe") if "ffprobe" not in missing_tools else None,
        },
        "device": config.selected_device if config else "unavailable",
        "model": MODEL_NAME,
        "weights_checksum": config.model_sha256 if config else None,
    }
    return V1CompatReadiness(status, tuple(missing_requirements), tuple(runtime_errors), runtime, config)
