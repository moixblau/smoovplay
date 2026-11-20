"""Streaming-optimised forward pass for the RIFE v4.2x IFNet family.

Same math as `IFNet.forward` in framegen/rife/*, restructured to cut full-resolution
memory traffic (which, not convolution FLOPs, dominates at 1080p+):

1. One warp per direction instead of two: the original warps `img` with flow_i at the
   end of step i and warps `f` with the same flow_i at the start of step i+1, so we warp
   a pre-concatenated [img | f] tensor once.
2. Each block's inputs are downsampled *before* concatenation (bilinear resize is
   per-channel, so this is exact) rather than concatenating ~28 channels at full res.
3. The fp32 copy needed for accurate grid_sample is made once per frame (`make_src`),
   not twice per warp.
4. Resizes by a factor of 1 are skipped.

Results match the reference forward to fp16 rounding (checked by scripts/check_fast.py).
"""

from __future__ import annotations

import torch
import torch.nn.functional as F


def _resize(t: torch.Tensor, factor: float) -> torch.Tensor:
    return t if factor == 1 else F.interpolate(t, scale_factor=factor, mode="bilinear")


# NHWC on NVIDIA (cuDNN's fast fp16 kernels); NCHW on AMD, where MIOpen copies NHWC input back to NCHW around
# each conv (halves RIFE's time at 1080p on an RX 9070 XT).
_LAYOUT = torch.contiguous_format if torch.version.hip else torch.channels_last


class FastIFNet:
    def __init__(self, net: torch.nn.Module, ph: int, pw: int, dtype: torch.dtype, device: torch.device) -> None:
        self.blocks = [getattr(net, f"block{i}").to(memory_format=_LAYOUT)
                       for i in range(len(net.scale_list))]
        self.scales = list(net.scale_list)
        self.dtype = dtype
        gx = torch.linspace(-1.0, 1.0, pw, device=device).view(1, 1, pw).expand(1, ph, pw)
        gy = torch.linspace(-1.0, 1.0, ph, device=device).view(1, ph, 1).expand(1, ph, pw)
        self.base_grid = torch.stack([gx, gy], dim=-1).contiguous()  # (1,H,W,2) for grid_sample
        self.flow_to_grid = torch.tensor([2.0 / (pw - 1), 2.0 / (ph - 1)], device=device)
        # Timestep is a constant plane (0.5 = midpoint); its bilinear downsample is the same constant.
        # Static tensors, so CUDA graphs read whatever set_timestep last wrote.
        self.timestep = {}
        for s in self.scales:
            probe = _resize(torch.zeros(1, 1, ph, pw, device=device), 1.0 / s)
            self.timestep[s] = torch.full(probe.shape, 0.5, dtype=dtype, device=device)

    def set_timestep(self, t: float) -> None:
        for plane in self.timestep.values():
            plane.fill_(t)

    @staticmethod
    def make_src(img: torch.Tensor, feat: torch.Tensor) -> torch.Tensor:
        """Per-frame [img(3) | encoder features] in fp32, ready for warping."""
        return torch.cat([img, feat], 1).float()

    def _warp(self, src: torch.Tensor, flow: torch.Tensor) -> torch.Tensor:
        grid = flow.permute(0, 2, 3, 1).float() * self.flow_to_grid + self.base_grid
        return F.grid_sample(src, grid, mode="bilinear", padding_mode="border", align_corners=True)

    @torch.no_grad()
    def __call__(self, src0: torch.Tensor, src1: torch.Tensor, t: torch.Tensor | None = None) -> torch.Tensor:
        """Frame at the current timestep (1,3,H,W) fp32 in 0..1 (unclamped) between two `make_src` tensors.

        t: optional (1,1,1,1) timestep tensor (the ONNX/TensorRT export takes it as an input) instead of the
        planes set by set_timestep."""
        dt = self.dtype
        flow = mask = feat = None
        w0, w1 = src0, src1  # at step 0 nothing is warped yet
        n = len(self.blocks)
        for i, (blk, s) in enumerate(zip(self.blocks, self.scales)):
            a = _resize(w0, 1.0 / s).to(dt)
            b = _resize(w1, 1.0 / s).to(dt)
            ts = self.timestep[s] if t is None else t.to(dt).expand(1, 1, a.shape[2], a.shape[3])
            parts = [a[:, :3], b[:, :3], a[:, 3:], b[:, 3:], ts]
            if flow is not None:
                parts += [_resize(mask, 1.0 / s), _resize(feat, 1.0 / s), _resize(flow, 1.0 / s) / s]
            x = torch.cat(parts, 1).contiguous(memory_format=_LAYOUT)
            tmp = blk.lastconv(blk.convblock(blk.conv0(x))).contiguous()
            tmp = _resize(tmp, s)
            fd = tmp[:, :4] * s
            flow = fd if flow is None else flow + fd
            mask, feat = tmp[:, 4:5], tmp[:, 5:]
            last = i == n - 1
            # On the final step only the images are needed, not the warped features.
            w0 = self._warp(src0[:, :3] if last else src0, flow[:, :2])
            w1 = self._warp(src1[:, :3] if last else src1, flow[:, 2:4])
        m = torch.sigmoid(mask.float())
        return w0 * m + w1 * (1 - m)
