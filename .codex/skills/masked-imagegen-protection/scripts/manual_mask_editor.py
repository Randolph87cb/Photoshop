import argparse
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


WINDOW_NAME = "保护区遮罩编辑器"
MAX_HISTORY = 20
SOFT_PROTECT_VALUE = 180
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/Deng.ttf"),
    Path("C:/Windows/Fonts/simsunb.ttf"),
]


def load_rgb(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


def save_rgb(path: Path, arr: np.ndarray) -> None:
    Image.fromarray(arr).save(path)


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


class MaskEditor:
    def __init__(self, image_path: Path, composite_path: Path | None, out_dir: Path) -> None:
        self.image_path = image_path
        self.rgb = load_rgb(image_path)
        self.height, self.width = self.rgb.shape[:2]
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.composite_rgb = None
        if composite_path:
            self.composite_rgb = load_rgb(composite_path)
            if self.composite_rgb.shape[:2] != self.rgb.shape[:2]:
                self.composite_rgb = np.array(
                    Image.fromarray(self.composite_rgb).resize((self.width, self.height), Image.Resampling.LANCZOS)
                )

        self.mask = np.zeros((self.height, self.width), dtype=np.uint8)
        self.auto_mask = self.mask.copy()
        self.history: list[np.ndarray] = []
        self.mode = "overlay"
        self.brush = 18
        self.drawing = False
        self.draw_value = SOFT_PROTECT_VALUE
        self.zoom_levels = [0.5, 0.75, 1.0, 1.5, 2.0]
        self.zoom_index = 2

    def push_history(self) -> None:
        self.history.append(self.mask.copy())
        if len(self.history) > MAX_HISTORY:
            self.history.pop(0)

    def undo(self) -> None:
        if self.history:
            self.mask = self.history.pop()

    def display_scale(self) -> float:
        return self.zoom_levels[self.zoom_index]

    def draw_circle(self, x: int, y: int, value: int) -> None:
        scale = self.display_scale()
        real_x = int(round(x / scale))
        real_y = int(round(y / scale))
        real_brush = max(1, int(round(self.brush / scale)))
        cv2.circle(self.mask, (real_x, real_y), real_brush, value, -1, lineType=cv2.LINE_AA)

    def render(self) -> np.ndarray:
        protect_alpha = (self.mask.astype(np.float32) / 255.0)[..., None]

        if self.mode == "mask":
            base = np.repeat(self.mask[:, :, None], 3, axis=2)
        elif self.mode == "composite" and self.composite_rgb is not None:
            base = (
                self.rgb.astype(np.float32) * protect_alpha
                + self.composite_rgb.astype(np.float32) * (1.0 - protect_alpha)
            ).clip(0, 255).astype(np.uint8)
        elif self.mode == "original":
            base = self.rgb.copy()
        else:
            tint = np.zeros_like(self.rgb)
            tint[:, :, 0] = self.mask
            tint[:, :, 1] = self.mask
            base = (
                self.rgb.astype(np.float32) * 0.6
                + tint.astype(np.float32) * 0.4
            ).clip(0, 255).astype(np.uint8)

        scale = self.display_scale()
        display = cv2.resize(
            cv2.cvtColor(base, cv2.COLOR_RGB2BGR),
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_LINEAR,
        )

        mode_map = {
            "overlay": "叠加预览",
            "composite": "合成预览",
            "original": "原图",
            "mask": "纯遮罩",
        }
        tips = [
            f"模式={mode_map.get(self.mode, self.mode)}",
            f"画笔={self.brush}",
            f"缩放={scale:.2f}x",
            "左键 添加灰色联动区",
            "右键 擦除到黑色可编辑区",
            "1 叠加预览",
            "2 合成预览",
            "3 原图",
            "4 纯遮罩",
            "[ ] 调整画笔",
            "- = 调整缩放",
            "u 撤销",
            "r 恢复空白遮罩",
            "c 清空遮罩",
            "s 保存",
            "q 退出",
        ]
        return draw_text_block(display, tips, (12, 12), size=20)

    def save(self) -> None:
        stem = self.image_path.stem
        mask_path = self.out_dir / f"{stem}-manual-mask.png"
        api_mask_path = self.out_dir / f"{stem}-manual-api-mask.png"
        overlay_path = self.out_dir / f"{stem}-manual-mask-overlay.png"
        composite_path = self.out_dir / f"{stem}-manual-composite-preview.jpg"

        Image.fromarray(self.mask).save(mask_path)

        # 透明像素代表可编辑区；本工具内部以灰色表示联动区，因此联动区保持半透明到不透明。
        alpha = self.mask.astype(np.uint8)
        rgba = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        rgba[:, :, 3] = alpha
        Image.fromarray(rgba, mode="RGBA").save(api_mask_path)

        tint = np.zeros_like(self.rgb)
        tint[:, :, 0] = self.mask
        tint[:, :, 1] = self.mask
        overlay = (
            self.rgb.astype(np.float32) * 0.6
            + tint.astype(np.float32) * 0.4
        ).clip(0, 255).astype(np.uint8)
        save_rgb(overlay_path, overlay)

        if self.composite_rgb is not None:
            protect_alpha = (self.mask.astype(np.float32) / 255.0)[..., None]
            composite = (
                self.rgb.astype(np.float32) * protect_alpha
                + self.composite_rgb.astype(np.float32) * (1.0 - protect_alpha)
            ).clip(0, 255).astype(np.uint8)
            save_rgb(composite_path, composite)

        print(f"已保存遮罩: {mask_path}")
        print(f"已保存带 alpha 遮罩: {api_mask_path}")
        print(f"已保存叠加预览: {overlay_path}")
        if self.composite_rgb is not None:
            print(f"已保存合成预览: {composite_path}")

    def on_mouse(self, event: int, x: int, y: int, flags: int, param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self.push_history()
            self.drawing = True
            self.draw_value = SOFT_PROTECT_VALUE
            self.draw_circle(x, y, self.draw_value)
        elif event == cv2.EVENT_RBUTTONDOWN:
            self.push_history()
            self.drawing = True
            self.draw_value = 0
            self.draw_circle(x, y, self.draw_value)
        elif event == cv2.EVENT_MOUSEMOVE and self.drawing:
            self.draw_circle(x, y, self.draw_value)
        elif event in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP):
            self.drawing = False

    def run(self) -> None:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(WINDOW_NAME, self.on_mouse)

        while True:
            cv2.imshow(WINDOW_NAME, self.render())
            key = cv2.waitKey(20) & 0xFF

            if key == ord("q"):
                break
            if key == ord("1"):
                self.mode = "overlay"
            elif key == ord("2") and self.composite_rgb is not None:
                self.mode = "composite"
            elif key == ord("3"):
                self.mode = "original"
            elif key == ord("4"):
                self.mode = "mask"
            elif key == ord("["):
                self.brush = max(2, self.brush - 2)
            elif key == ord("]"):
                self.brush = min(200, self.brush + 2)
            elif key in (ord("-"), ord("_")):
                self.zoom_index = max(0, self.zoom_index - 1)
            elif key in (ord("="), ord("+")):
                self.zoom_index = min(len(self.zoom_levels) - 1, self.zoom_index + 1)
            elif key == ord("u"):
                self.undo()
            elif key == ord("r"):
                self.push_history()
                self.mask = self.auto_mask.copy()
            elif key == ord("c"):
                self.push_history()
                self.mask = np.zeros_like(self.mask)
            elif key == ord("s"):
                self.save()

        cv2.destroyAllWindows()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="手工灰黑分区遮罩编辑器。")
    parser.add_argument("image", type=Path, help="原图路径。")
    parser.add_argument(
        "--composite-with",
        type=Path,
        default=None,
        help="可选生成图，用于查看黑区替换后的合成预览。",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("mask-output"),
        help="遮罩和预览图输出目录。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    editor = MaskEditor(args.image, args.composite_with, args.out_dir)
    editor.run()


if __name__ == "__main__":
    main()
