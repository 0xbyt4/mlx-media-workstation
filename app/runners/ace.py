"""ACE-Step 1.5 runner (runs in the checkout's .venv, from the checkout folder, offline): music from a description.

Uses the pinned checkout's Python API with the MLX DiT and the MLX 5Hz LM 1.7B (no server, no port). Writes
take1.wav, take2.wav, ... and notes.json (seed, key, BPM).

Memory (measured 2026-10-24, 20 s x 2 takes: peak footprint 42.9 GB -> 21.5 GB, output bit-identical):
- the MLX buffer cache is capped at 2 GB (by default it keeps every freed buffer: -11 GB);
- the PyTorch DiT decoder is dropped once the MLX copy exists (it is only a fallback then: -5 GB);
- the MLX DiT weights go back to bfloat16, as stored in the checkpoint (the loader upcasts to fp32: -2 GB);
- the LM is unloaded after it wrote the audio codes, before the DiT and the VAE run (-4 GB).
A given seed also seeds MLX and torch globally: without it the LM's sampling differs on every run.

  python ace.py job.json
"""

import functools
import gc
import json
import shutil
import sys
from pathlib import Path

import mlx.core as mx
import torch
from mlx.utils import tree_map

ACE = Path.cwd()  # the job runs from the ACE-Step checkout (config.toml: ace_dir)
sys.path.insert(0, str(ACE))
from acestep.handler import AceStepHandler
from acestep.inference import GenerationConfig, GenerationParams, generate_music
from acestep.llm_inference import LLMHandler

CACHE_LIMIT = 2 * 10**9


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text())
    out = Path(job["out"])
    if job.get("seed") is not None:
        mx.random.seed(int(job["seed"]))
        torch.manual_seed(int(job["seed"]))
    mx.set_cache_limit(CACHE_LIMIT)
    dit, llm = AceStepHandler(), LLMHandler()
    dit.initialize_service(
        project_root=str(ACE), config_path="acestep-v15-turbo", device="auto"
    )
    if dit.use_mlx_dit:
        dit.model.decoder = None
        dit.mlx_decoder.update(
            tree_map(
                lambda a: a.astype(mx.bfloat16) if a.dtype == mx.float32 else a,
                dit.mlx_decoder.parameters(),
            )
        )
        mx.eval(dit.mlx_decoder.parameters())
        gc.collect()
        torch.mps.empty_cache()
        mx.clear_cache()
    llm.initialize(
        checkpoint_dir=str(ACE / "checkpoints"),
        lm_model_path="acestep-5Hz-lm-1.7B",
        backend="mlx",
        device="auto",
    )
    inner = dit.generate_music

    @functools.wraps(inner)  # generate_music reads this signature to pick its arguments
    def generate_after_lm(*args, **kwargs):
        llm.unload()  # the LM is done once the DiT starts: it only writes the codes
        gc.collect()
        mx.clear_cache()
        return inner(*args, **kwargs)

    dit.generate_music = generate_after_lm
    params = GenerationParams(
        caption=job["caption"],
        instrumental=job["instrumental"],
        lyrics="[Instrumental]" if job["instrumental"] else job["lyrics"],
        duration=float(job["duration"]),
        bpm=int(job["bpm"]) if job.get("bpm") else None,
        keyscale=job.get("key") or "",
    )
    seeds = (
        [int(job["seed"]) + i for i in range(job["takes"])]
        if job.get("seed") is not None
        else None
    )
    config = GenerationConfig(
        batch_size=job["takes"],
        audio_format="wav",
        use_random_seed=seeds is None,
        seeds=seeds,
    )
    res = generate_music(dit, llm, params, config, save_dir=str(out / "ace_raw"))
    if not res.success:
        raise SystemExit(f"ACE-Step failed: {res.error}")
    notes = {}
    for i, a in enumerate(res.audios, start=1):
        name = f"take{i}.wav"
        shutil.move(a["path"], out / name)
        p = a.get("params", {})
        notes[name] = (
            f"seed {p.get('seed')}, bpm {p.get('bpm') or p.get('cot_bpm') or 'auto'}, key {p.get('keyscale') or p.get('cot_keyscale') or 'auto'}"
        )
    shutil.rmtree(out / "ace_raw", ignore_errors=True)
    (out / "notes.json").write_text(json.dumps(notes, indent=1))
    print(f"{len(res.audios)} takes written")


if __name__ == "__main__":
    main()
