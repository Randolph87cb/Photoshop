import argparse
import json
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from apply_protection_overlay import soften_alpha, soften_seam, tone_match_protected_region


WINDOW_NAME = "灰区联通块对齐器"
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/Deng.ttf"),
    Path("C:/Windows/Fonts/simsunb.ttf"),
]


def load_rgb(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


def load_mask_alpha(path: Path) -> np.ndarray:
    img = Image.open(path)
    if img.mode == "RGBA":
        return np.array(img.getchannel("A"), dtype=np.uint8)
    return np.array(img.convert("L"), dtype=np.uint8)


@lru_cache(maxsize=8)
def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in FONT_CANDIDATES:
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_text_block(image_bgr: np.ndarray, lines: list[str], start_xy: tuple[int, int], size: int = 22) -> np.ndarray:
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    font = load_font(size)
    x, y = start_xy
    line_height = size + 8
    for line in lines:
        draw.text((x, y), line, font=font, fill=(245, 245, 245), stroke_width=2, stroke_fill=(20, 20, 20))
        y += line_height
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def build_connected_components(alpha: np.ndarray, threshold: int) -> tuple[int, np.ndarray, np.ndarray]:
    binary = (alpha >= threshold).astype(np.uint8)
    return cv2.connectedComponentsWithStats(binary, connectivity=8)


def translate_mask(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    height, width = mask.shape
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        mask.astype(np.uint8),
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def translate_rgb(region: np.ndarray, dx: int, dy: int) -> np.ndarray:
    height, width = region.shape[:2]
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        region,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )


def component_score(
    original: np.ndarray,
    generated: np.ndarray,
    component_mask: np.ndarray,
    dx: int,
    dy: int,
) -> float:
    moved_mask = translate_mask(component_mask, dx, dy)
    active = moved_mask > 0
    if not np.any(active):
        return float("inf")

    component_rgb = np.where(component_mask[:, :, None] > 0, original, 0).astype(np.uint8)
    moved_rgb = translate_rgb(component_rgb, dx, dy)
    diff = np.abs(moved_rgb[active].astype(np.int32) - generated[active].astype(np.int32))
    return float(diff.sum())


def merge_overlay(
    original: np.ndarray,
    generated: np.ndarray,
    alpha: np.ndarray,
    threshold: int,
    match_tones: bool,
    match_erode: int,
    feather: float,
    seam_blur: float,
    seam_width: int,
) -> tuple[np.ndarray, dict[str, object]]:
    overlay_source = original
    stats: dict[str, object] = {}
    if match_tones:
        overlay_source, stats = tone_match_protected_region(
            overlay_source,
            generated,
            alpha,
            match_erode,
            threshold,
        )

    protect_alpha = soften_alpha(alpha, feather, threshold)[..., None]
    merged = (
        overlay_source.astype(np.float32) * protect_alpha
        + generated.astype(np.float32) * (1.0 - protect_alpha)
    ).clip(0, 255).astype(np.uint8)
    merged = soften_seam(
        merged,
        alpha,
        threshold,
        seam_width,
        seam_blur,
    )
    return merged, stats


class RegionAligner:
    def __init__(
        self,
        original_path: Path,
        generated_path: Path,
        mask_path: Path,
        threshold: int,
        auto_range: int,
        out_json: Path,
        out_preview: Path,
        auto_out: Path | None,
        match_tones: bool,
        match_erode: int,
        feather: float,
        seam_blur: float,
        seam_width: int,
    ) -> None:
        self.original_path = original_path
        self.generated_path = generated_path
        self.mask_path = mask_path
        self.threshold = threshold
        self.auto_range = auto_range
        self.out_json = out_json
        self.out_preview = out_preview
        self.auto_out = auto_out
        self.match_tones = match_tones
        self.match_erode = match_erode
        self.feather = feather
        self.seam_blur = seam_blur
        self.seam_width = seam_width

        self.original = load_rgb(original_path)
        self.generated = load_rgb(generated_path)
        self.alpha = load_mask_alpha(mask_path)

        height, width = self.original.shape[:2]
        if self.generated.shape[:2] != (height, width):
            self.generated = np.array(
                Image.fromarray(self.generated).resize((width, height), Image.Resampling.LANCZOS)
            )
        if self.alpha.shape != (height, width):
            self.alpha = np.array(Image.fromarray(self.alpha).resize((width, height), Image.Resampling.NEAREST))

        self.num_labels, self.labels, self.stats, _ = build_connected_components(self.alpha, self.threshold)
        self.region_labels = [
            label
            for label in range(1, self.num_labels)
            if int(self.stats[label, cv2.CC_STAT_AREA]) >= 64
        ]
        if not self.region_labels:
            raise ValueError("当前遮罩里没有可用的灰区联通块。")

        self.current_index = 0
        self.zoom_levels = [0.5, 0.75, 1.0, 1.5, 2.0]
        self.zoom_index = 2
        self.overlay_alpha = 0.72
        self.show_edges = True
        self.auto_records: list[dict[str, object]] = []
        self.auto_tone_stats: dict[str, object] = {}
        self.base_transforms = self.auto_align_regions()
        self.transforms: dict[int, dict[str, int]] = {
            label: values.copy()
            for label, values in self.base_transforms.items()
        }
        self.save_outputs(auto_stage=True)

    def display_scale(self) -> float:
        return self.zoom_levels[self.zoom_index]

    def current_label(self) -> int:
        return self.region_labels[self.current_index]

    def component_mask(self, label: int) -> np.ndarray:
        return np.where(self.labels == label, self.alpha, 0).astype(np.uint8)

    def move_current(self, dx: int, dy: int) -> None:
        label = self.current_label()
        self.transforms[label]["dx"] += dx
        self.transforms[label]["dy"] += dy

    def reset_current(self) -> None:
        label = self.current_label()
        self.transforms[label] = self.base_transforms[label].copy()

    def reset_all(self) -> None:
        for label in self.region_labels:
            self.transforms[label] = self.base_transforms[label].copy()

    def auto_align_regions(self) -> dict[int, dict[str, int]]:
        transforms: dict[int, dict[str, int]] = {}
        for label in self.region_labels:
            mask = self.component_mask(label)
            best_dx = 0
            best_dy = 0
            best_score = component_score(self.original, self.generated, mask, 0, 0)

            for dy in range(-self.auto_range, self.auto_range + 1):
                for dx in range(-self.auto_range, self.auto_range + 1):
                    score = component_score(self.original, self.generated, mask, dx, dy)
                    if score < best_score:
                        best_score = score
                        best_dx = dx
                        best_dy = dy

            transforms[label] = {"dx": best_dx, "dy": best_dy}
            self.auto_records.append(
                {
                    "label": int(label),
                    "area": int(self.stats[label, cv2.CC_STAT_AREA]),
                    "dx": int(best_dx),
                    "dy": int(best_dy),
                    "score": round(best_score, 2),
                }
            )
        return transforms

    def apply_transforms(self) -> tuple[np.ndarray, np.ndarray]:
        aligned = self.original.copy()
        aligned_alpha = np.zeros_like(self.alpha, dtype=np.uint8)
        for label in self.region_labels:
            mask = self.component_mask(label)
            dx = self.transforms[label]["dx"]
            dy = self.transforms[label]["dy"]
            moved_alpha = translate_mask(mask, dx, dy)
            region = np.where(mask[:, :, None] > 0, self.original, 0).astype(np.uint8)
            moved_region = translate_rgb(region, dx, dy)
            moved_pixels = moved_alpha > 0
            aligned[moved_pixels] = moved_region[moved_pixels]
            aligned_alpha = np.maximum(aligned_alpha, moved_alpha)
        return aligned, aligned_alpha

    def render(self) -> np.ndarray:
        aligned, aligned_alpha = self.apply_transforms()
        alpha_3 = (aligned_alpha.astype(np.float32) / 255.0 * self.overlay_alpha)[..., None]
        display_rgb = (
            aligned.astype(np.float32) * alpha_3
            + self.generated.astype(np.float32) * (1.0 - alpha_3)
        ).clip(0, 255).astype(np.uint8)

        if self.show_edges:
            all_edges = cv2.Canny((aligned_alpha > 0).astype(np.uint8) * 255, 50, 150)
            display_rgb[all_edges > 0] = [255, 220, 0]

            current_mask = self.component_mask(self.current_label())
            dx = self.transforms[self.current_label()]["dx"]
            dy = self.transforms[self.current_label()]["dy"]
            current_moved = translate_mask(current_mask, dx, dy)
            current_edges = cv2.Canny((current_moved > 0).astype(np.uint8) * 255, 50, 150)
            display_rgb[current_edges > 0] = [40, 255, 40]

        display_bgr = cv2.cvtColor(display_rgb, cv2.COLOR_RGB2BGR)
        scale = self.display_scale()
        if scale != 1.0:
            display_bgr = cv2.resize(
                display_bgr,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_LINEAR,
            )

        label = self.current_label()
        area = int(self.stats[label, cv2.CC_STAT_AREA])
        dx = self.transforms[label]["dx"]
        dy = self.transforms[label]["dy"]
        auto_dx = self.base_transforms[label]["dx"]
        auto_dy = self.base_transforms[label]["dy"]
        tips = [
            f"联通块 {self.current_index + 1}/{len(self.region_labels)}  标签={label}  面积={area}",
            f"当前位移 dx={dx} dy={dy}  自动初值 dx={auto_dx} dy={auto_dy}",
            f"缩放={scale:.2f}x  叠加强度={self.overlay_alpha:.2f}",
            f"自动预对齐范围: 上下左右各 {self.auto_range} 像素",
            "显示方式: 原图灰区叠到生成图上, 绿边=当前块, 黄边=全部灰区",
            "鼠标左键 选中当前联通块",
            "w a s d 当前块移动 1 像素",
            "W A S D 当前块移动 5 像素",
            "n / p 切换联通块",
            "r 重置当前块到自动对齐  0 重置全部到自动对齐",
            "[ ] 调叠加强度  - = 调缩放",
            "e 切换边缘显示",
            "v 保存参数和预览  q 退出",
        ]
        return draw_text_block(display_bgr, tips, (12, 12), size=20)

    def save_outputs(self, auto_stage: bool) -> None:
        aligned, aligned_alpha = self.apply_transforms()
        preview_alpha = (aligned_alpha.astype(np.float32) / 255.0 * self.overlay_alpha)[..., None]
        preview = (
            aligned.astype(np.float32) * preview_alpha
            + self.generated.astype(np.float32) * (1.0 - preview_alpha)
        ).clip(0, 255).astype(np.uint8)

        self.out_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "original": str(self.original_path),
            "generated": str(self.generated_path),
            "mask": str(self.mask_path),
            "threshold": self.threshold,
            "auto_range": self.auto_range,
            "auto_records": self.auto_records,
            "transforms": self.transforms,
        }
        self.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        Image.fromarray(preview).save(self.out_preview)
        stage_label = "自动对齐" if auto_stage else "手动对齐"
        print(f"已保存{stage_label}位移参数: {self.out_json}")
        print(f"已保存{stage_label}预览图: {self.out_preview}")

        if self.auto_out is not None:
            merged, tone_stats = merge_overlay(
                aligned,
                self.generated,
                aligned_alpha,
                self.threshold,
                self.match_tones,
                self.match_erode,
                self.feather,
                self.seam_blur,
                self.seam_width,
            )
            self.auto_out.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(merged).save(self.auto_out)
            if auto_stage:
                self.auto_tone_stats = tone_stats
            print(f"已保存{stage_label}回盖图: {self.auto_out}")
            if tone_stats:
                print(f"{stage_label}色调匹配统计: {tone_stats}")

    def save(self) -> None:
        self.save_outputs(auto_stage=False)

    def on_mouse(self, event: int, x: int, y: int, flags: int, param: object) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        scale = self.display_scale()
        real_x = int(round(x / scale))
        real_y = int(round(y / scale))
        if real_x < 0 or real_y < 0 or real_x >= self.alpha.shape[1] or real_y >= self.alpha.shape[0]:
            return

        for index, label in enumerate(self.region_labels):
            mask = self.component_mask(label)
            dx = self.transforms[label]["dx"]
            dy = self.transforms[label]["dy"]
            moved_mask = translate_mask(mask, dx, dy)
            if moved_mask[real_y, real_x] > 0:
                self.current_index = index
                break

    def run(self) -> None:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(WINDOW_NAME, self.on_mouse)
        while True:
            cv2.imshow(WINDOW_NAME, self.render())
            key = cv2.waitKey(20) & 0xFF
            if key == ord("q"):
                break
            if key == ord("n"):
                self.current_index = (self.current_index + 1) % len(self.region_labels)
            elif key == ord("p"):
                self.current_index = (self.current_index - 1) % len(self.region_labels)
            elif key == ord("w"):
                self.move_current(0, -1)
            elif key == ord("a"):
                self.move_current(-1, 0)
            elif key == ord("d"):
                self.move_current(1, 0)
            elif key == ord("s"):
                self.move_current(0, 1)
            elif key == ord("W"):
                self.move_current(0, -5)
            elif key == ord("A"):
                self.move_current(-5, 0)
            elif key == ord("D"):
                self.move_current(5, 0)
            elif key == ord("S"):
                self.move_current(0, 5)
            elif key == ord("v"):
                self.save()
            elif key == ord("r"):
                self.reset_current()
            elif key == ord("0"):
                self.reset_all()
            elif key == ord("e"):
                self.show_edges = not self.show_edges
            elif key == ord("["):
                self.overlay_alpha = max(0.15, self.overlay_alpha - 0.05)
            elif key == ord("]"):
                self.overlay_alpha = min(1.0, self.overlay_alpha + 0.05)
            elif key in (ord("-"), ord("_")):
                self.zoom_index = max(0, self.zoom_index - 1)
            elif key in (ord("="), ord("+")):
                self.zoom_index = min(len(self.zoom_levels) - 1, self.zoom_index + 1)
        cv2.destroyAllWindows()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按灰区联通块微调原图位置，对齐生成图后再做合并。")
    parser.add_argument("--original", type=Path, required=True, help="原图路径。")
    parser.add_argument("--generated", type=Path, required=True, help="raw 生成图路径。")
    parser.add_argument("--mask", type=Path, required=True, help="灰黑分区遮罩路径。")
    parser.add_argument("--threshold", type=int, default=32, help="多少以上的遮罩值会被视为灰区联通块。")
    parser.add_argument(
        "--auto-range",
        type=int,
        default=20,
        help="自动预对齐时，dx 和 dy 各自搜索的像素范围，默认上下左右各 20 像素。",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="位移参数 JSON 输出路径，默认与遮罩放在同目录。",
    )
    parser.add_argument(
        "--out-preview",
        type=Path,
        default=None,
        help="对齐预览图输出路径，默认与遮罩放在同目录。",
    )
    parser.add_argument(
        "--auto-out",
        type=Path,
        default=None,
        help="自动预对齐后直接生成的一版回盖图输出路径。",
    )
    parser.add_argument(
        "--match-tones",
        action="store_true",
        help="自动预对齐回盖图时，同时按联通块做亮度和色偏匹配。",
    )
    parser.add_argument(
        "--match-erode",
        type=int,
        default=4,
        help="自动预对齐回盖图做色调匹配时先向内收缩多少像素。",
    )
    parser.add_argument(
        "--feather",
        type=float,
        default=1.0,
        help="自动预对齐回盖图时，对联动区 alpha 做轻微羽化。",
    )
    parser.add_argument(
        "--seam-blur",
        type=float,
        default=0.0,
        help="自动预对齐回盖图时，只对接缝带做局部模糊。",
    )
    parser.add_argument(
        "--seam-width",
        type=int,
        default=3,
        help="自动预对齐回盖图的接缝带半宽，单位像素。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stem = args.original.stem
    out_json = args.out_json or args.mask.parent / f"{stem}-region-transforms.json"
    out_preview = args.out_preview or args.mask.parent / f"{stem}-alignment-preview.png"
    if args.auto_out is not None:
        auto_out = args.auto_out
    elif args.generated.stem.endswith("-raw"):
        auto_out = args.generated.with_name(args.generated.stem[:-4] + "-auto-aligned" + args.generated.suffix)
    else:
        auto_out = args.generated.with_name(args.generated.stem + "-auto-aligned" + args.generated.suffix)
    aligner = RegionAligner(
        args.original,
        args.generated,
        args.mask,
        args.threshold,
        args.auto_range,
        out_json,
        out_preview,
        auto_out,
        args.match_tones,
        args.match_erode,
        args.feather,
        args.seam_blur,
        args.seam_width,
    )
    aligner.run()


if __name__ == "__main__":
    main()
