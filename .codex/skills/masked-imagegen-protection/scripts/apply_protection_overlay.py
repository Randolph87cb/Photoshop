import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def load_rgb(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


def load_mask_alpha(path: Path) -> np.ndarray:
    img = Image.open(path)
    if img.mode == "RGBA":
        alpha = np.array(img.getchannel("A"), dtype=np.uint8)
        return alpha
    return np.array(img.convert("L"), dtype=np.uint8)


def clipped_stats(values: np.ndarray) -> tuple[float, float]:
    if values.size == 0:
        return 0.0, 1.0
    lo = np.percentile(values, 5)
    hi = np.percentile(values, 95)
    clipped = values[(values >= lo) & (values <= hi)]
    if clipped.size == 0:
        clipped = values
    mean = float(clipped.mean())
    std = float(clipped.std())
    return mean, max(std, 1e-6)


def build_connected_components(alpha: np.ndarray, threshold: int) -> tuple[int, np.ndarray, np.ndarray]:
    binary = (alpha >= threshold).astype(np.uint8)
    return cv2.connectedComponentsWithStats(binary, connectivity=8)


def load_region_transforms(path: Path | None) -> dict[int, tuple[int, int]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("transforms", {})
    transforms: dict[int, tuple[int, int]] = {}
    for key, value in raw.items():
        try:
            label = int(key)
        except ValueError:
            continue
        dx = int(value.get("dx", 0))
        dy = int(value.get("dy", 0))
        transforms[label] = (dx, dy)
    return transforms


def translate_component(
    image: np.ndarray,
    mask: np.ndarray,
    dx: int,
    dy: int,
) -> tuple[np.ndarray, np.ndarray]:
    height, width = mask.shape
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    moved_image = cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    moved_mask = cv2.warpAffine(
        mask,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return moved_image, moved_mask


def apply_region_transforms(
    original: np.ndarray,
    alpha: np.ndarray,
    threshold: int,
    transforms: dict[int, tuple[int, int]],
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    num_labels, labels, stats, _ = build_connected_components(alpha, threshold)
    if num_labels <= 1 or not transforms:
        return original.copy(), alpha.copy(), {"components": 0, "transforms": []}

    aligned_original = original.copy()
    aligned_alpha = np.zeros_like(alpha, dtype=np.uint8)
    records: list[dict[str, int]] = []

    for label in range(1, num_labels):
        component_mask = np.where(labels == label, alpha, 0).astype(np.uint8)
        if not np.any(component_mask):
            continue

        dx, dy = transforms.get(label, (0, 0))
        component_rgb = np.where(component_mask[:, :, None] > 0, original, 0).astype(np.uint8)
        moved_rgb, moved_alpha = translate_component(component_rgb, component_mask, dx, dy)

        moved_region = moved_alpha > 0
        aligned_original[moved_region] = moved_rgb[moved_region]
        aligned_alpha = np.maximum(aligned_alpha, moved_alpha)

        if dx != 0 or dy != 0:
            records.append(
                {
                    "label": int(label),
                    "area": int(stats[label, cv2.CC_STAT_AREA]),
                    "dx": int(dx),
                    "dy": int(dy),
                }
            )

    if not np.any(aligned_alpha):
        aligned_alpha = alpha.copy()

    return aligned_original, aligned_alpha, {"components": len(records), "transforms": records}


def tone_match_protected_region(
    original: np.ndarray,
    generated: np.ndarray,
    alpha: np.ndarray,
    erode_px: int,
    threshold: int,
) -> tuple[np.ndarray, dict[str, float]]:
    original_lab = cv2.cvtColor(original, cv2.COLOR_RGB2LAB).astype(np.float32)
    generated_lab = cv2.cvtColor(generated, cv2.COLOR_RGB2LAB).astype(np.float32)
    adjusted_lab = original_lab.copy()

    binary = (alpha >= threshold).astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num_labels <= 1:
        return original.copy(), {"matched_pixels": 0, "components": 0}

    matched_pixels = 0
    component_count = 0
    records: list[dict[str, float]] = []
    kernel = None
    if erode_px > 0:
        kernel = np.ones((erode_px * 2 + 1, erode_px * 2 + 1), np.uint8)

    for label in range(1, num_labels):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < 64:
            continue

        component_mask = (labels == label).astype(np.uint8)
        stats_mask = component_mask
        if kernel is not None:
            eroded = cv2.erode(component_mask, kernel, iterations=1)
            if int(eroded.sum()) > 0:
                stats_mask = eroded

        stats_region = stats_mask.astype(bool)
        apply_region = component_mask.astype(bool)
        if not np.any(stats_region):
            continue

        src_l = original_lab[:, :, 0][stats_region]
        dst_l = generated_lab[:, :, 0][stats_region]
        src_a = original_lab[:, :, 1][stats_region]
        dst_a = generated_lab[:, :, 1][stats_region]
        src_b = original_lab[:, :, 2][stats_region]
        dst_b = generated_lab[:, :, 2][stats_region]

        src_l_mean, src_l_std = clipped_stats(src_l)
        dst_l_mean, dst_l_std = clipped_stats(dst_l)
        l_scale = float(np.clip(dst_l_std / src_l_std, 0.75, 1.35))
        l_shift = float(dst_l_mean - src_l_mean * l_scale)

        src_a_mean, _ = clipped_stats(src_a)
        dst_a_mean, _ = clipped_stats(dst_a)
        src_b_mean, _ = clipped_stats(src_b)
        dst_b_mean, _ = clipped_stats(dst_b)
        a_shift = float(np.clip(dst_a_mean - src_a_mean, -10.0, 10.0))
        b_shift = float(np.clip(dst_b_mean - src_b_mean, -10.0, 10.0))

        adjusted_lab[:, :, 0][apply_region] = np.clip(
            adjusted_lab[:, :, 0][apply_region] * l_scale + l_shift, 0, 255
        )
        adjusted_lab[:, :, 1][apply_region] = np.clip(
            adjusted_lab[:, :, 1][apply_region] + a_shift, 0, 255
        )
        adjusted_lab[:, :, 2][apply_region] = np.clip(
            adjusted_lab[:, :, 2][apply_region] + b_shift, 0, 255
        )

        matched_pixels += area
        component_count += 1
        records.append(
            {
                "label": int(label),
                "area": area,
                "l_scale": round(l_scale, 4),
                "l_shift": round(l_shift, 4),
                "a_shift": round(a_shift, 4),
                "b_shift": round(b_shift, 4),
            }
        )

    adjusted = cv2.cvtColor(adjusted_lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
    return adjusted, {
        "matched_pixels": matched_pixels,
        "components": component_count,
        "component_stats": records,
    }


def soften_alpha(alpha: np.ndarray, feather_px: float, threshold: int) -> np.ndarray:
    binary = np.where(alpha >= threshold, 255.0, 0.0).astype(np.float32)
    if feather_px <= 0:
        return binary / 255.0
    kernel = max(3, int(round(feather_px * 4)) | 1)
    blurred = cv2.GaussianBlur(binary, (kernel, kernel), feather_px)
    return np.clip(blurred / 255.0, 0.0, 1.0)


def soften_seam(
    merged: np.ndarray,
    alpha: np.ndarray,
    threshold: int,
    seam_width: int,
    blur_sigma: float,
) -> np.ndarray:
    if seam_width <= 0 or blur_sigma <= 0:
        return merged

    binary = (alpha >= threshold).astype(np.uint8)
    kernel_size = max(3, seam_width * 2 + 1)
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    dilated = cv2.dilate(binary, kernel, iterations=1)
    eroded = cv2.erode(binary, kernel, iterations=1)
    seam_band = np.clip(dilated - eroded, 0, 1).astype(np.float32)
    if not np.any(seam_band):
        return merged

    blur_kernel = max(3, int(round(blur_sigma * 4)) | 1)
    blurred = cv2.GaussianBlur(merged, (blur_kernel, blur_kernel), blur_sigma)
    seam_alpha = seam_band[..., None]
    softened = (
        blurred.astype(np.float32) * seam_alpha
        + merged.astype(np.float32) * (1.0 - seam_alpha)
    ).clip(0, 255).astype(np.uint8)
    return softened


def main() -> None:
    parser = argparse.ArgumentParser(description="把原图联动区混合回生成图。")
    parser.add_argument("--original", type=Path, required=True, help="原图路径。")
    parser.add_argument("--generated", type=Path, required=True, help="生成结果图路径。")
    parser.add_argument("--mask", type=Path, required=True, help="灰黑分区遮罩路径。")
    parser.add_argument("--out", type=Path, required=True, help="输出图片路径。")
    parser.add_argument(
        "--region-transforms",
        type=Path,
        default=None,
        help="可选联通区域位移参数 JSON，用于先把原图灰区局部对齐后再合并。",
    )
    parser.add_argument(
        "--match-tones",
        action="store_true",
        help="根据 raw 生成图的灰色联动区统计，对原图联动区做亮度和色偏匹配后再混合回去。",
    )
    parser.add_argument(
        "--match-erode",
        type=int,
        default=3,
        help="做色调匹配时先向内收缩多少像素，避免边界污染。",
    )
    parser.add_argument(
        "--match-threshold",
        type=int,
        default=32,
        help="色调匹配时，多少以上的遮罩值会被视为联动区。",
    )
    parser.add_argument(
        "--feather",
        type=float,
        default=0.5,
        help="对联动区 alpha 做轻微羽化，减弱硬边界。",
    )
    parser.add_argument(
        "--seam-blur",
        type=float,
        default=1.0,
        help="只对灰区接缝带做局部模糊，压掉边缘发亮或发硬的一圈。",
    )
    parser.add_argument(
        "--seam-width",
        type=int,
        default=2,
        help="接缝带半宽，单位像素。",
    )
    args = parser.parse_args()

    original = load_rgb(args.original)
    generated = load_rgb(args.generated)
    if generated.shape[:2] != original.shape[:2]:
        generated = np.array(
            Image.fromarray(generated).resize((original.shape[1], original.shape[0]), Image.Resampling.LANCZOS)
        )

    alpha = load_mask_alpha(args.mask)
    if alpha.shape != original.shape[:2]:
        alpha = np.array(
            Image.fromarray(alpha).resize((original.shape[1], original.shape[0]), Image.Resampling.LANCZOS)
        )

    transforms = load_region_transforms(args.region_transforms)
    overlay_source, alpha, align_stats = apply_region_transforms(
        original,
        alpha,
        args.match_threshold,
        transforms,
    )
    if transforms:
        print(f"联通区域对齐统计: {align_stats}")

    if args.match_tones:
        overlay_source, stats = tone_match_protected_region(
            overlay_source,
            generated,
            alpha,
            args.match_erode,
            args.match_threshold,
        )
        print(f"色调匹配统计: {stats}")

    protect_alpha = soften_alpha(alpha, args.feather, args.match_threshold)[..., None]
    merged = (
        overlay_source.astype(np.float32) * protect_alpha
        + generated.astype(np.float32) * (1.0 - protect_alpha)
    ).clip(0, 255).astype(np.uint8)
    merged = soften_seam(
        merged,
        alpha,
        args.match_threshold,
        args.seam_width,
        args.seam_blur,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(merged).save(args.out)
    print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
