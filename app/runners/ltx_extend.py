"""LTX-2.5 extend runner (runs in the LTX environment, offline): the `ltx-2-mlx` CLI with lower memory peaks.

Takes the same arguments as `ltx-2-mlx`. It changes no installed file; it only wraps two functions before the CLI runs.
Memory (measured 2026-10-24 on 8 s extends from a 49-frame source):
- the source-video VAE encode runs with MLX's buffer cache off, as the engine already does for the decode; otherwise
  freed tile buffers stay parked in the cache between tiles (GPU memory in the encode: 33.9 GB -> 15.7 GB);
- the buffer cache is capped at 2 GB for the whole run (by default it keeps every freed buffer): footprint
  38.8 GB -> 34.2 GB, about 5 % slower, the video MD5-identical.

  python ltx_extend.py extend --distilled --model ... --video tail.mp4 --extend-frames 15 -o extended_raw.mp4
"""

import functools
import sys
from collections.abc import Callable

import mlx.core as mx
from ltx_core_mlx.model.video_vae.video_vae import decode_cache_limit
from ltx_pipelines_mlx import extend_distilled
from ltx_pipelines_mlx.cli import main

CACHE_LIMIT = 2 * 10**9


def without_cache[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with decode_cache_limit():
            return fn(*args, **kwargs)

    return run


extend_distilled.encode_video_tensor = without_cache(
    extend_distilled.encode_video_tensor
)
extend_distilled.encode_source_audio_latent = without_cache(
    extend_distilled.encode_source_audio_latent
)
mx.set_cache_limit(CACHE_LIMIT)

if __name__ == "__main__":
    sys.argv[0] = "ltx-2-mlx"
    sys.exit(main())
