import cv2
import numpy as np


class ImageFeatureExtractor:
    """Image preprocessing, local features, and ridge consistency metrics."""

    def __init__(self, upscale_factor=2):
        self.upscale_factor = upscale_factor
        self.detector = cv2.SIFT_create(
            nfeatures=0,
            nOctaveLayers=3,
            contrastThreshold=0.04,
            edgeThreshold=10,
            sigma=1.2,
        )
        self.descriptor_norm = cv2.NORM_L2

    def raw_frame_to_image(self, raw):
        return np.array(list(raw), dtype=np.uint8).reshape((52, 103))

    def preprocess(self, img_array):
        img = cv2.normalize(img_array, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
        img = cv2.equalizeHist(img)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        return img

    def detect_features(self, img):
        h, w = img.shape[:2]
        upscaled = cv2.resize(
            img,
            (w * self.upscale_factor, h * self.upscale_factor),
            interpolation=cv2.INTER_CUBIC,
        )
        kp, des = self.detector.detectAndCompute(upscaled, None)
        if kp is not None:
            scale = 1.0 / self.upscale_factor
            for k in kp:
                k.pt = (k.pt[0] * scale, k.pt[1] * scale)
                k.size = k.size * scale
        return kp, des

    def frame_quality(self, img):
        img_f = img.astype(np.float32)

        contrast = np.clip(float(np.std(img_f)) / 128.0, 0, 1)

        laplacian = cv2.Laplacian(img, cv2.CV_64F)
        sharpness = np.clip(laplacian.var() / 5000.0, 0, 1)

        fg_mask = (img > 20) & (img < 240)
        foreground = float(np.sum(fg_mask)) / float(img.size)

        gy, gx = np.gradient(img_f)
        clarity = np.clip(float(np.mean(np.sqrt(gx**2 + gy**2))) / 80.0, 0, 1)

        return contrast * 0.2 + sharpness * 0.3 + foreground * 0.2 + clarity * 0.3

    def orientation_descriptor(self, img, block_size=8):
        img_f = img.astype(np.float32)
        gx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)

        rows = []
        weights = []
        for y in range(0, img.shape[0] - block_size + 1, block_size):
            row = []
            weight_row = []
            for x in range(0, img.shape[1] - block_size + 1, block_size):
                bx = gx[y:y + block_size, x:x + block_size]
                by = gy[y:y + block_size, x:x + block_size]
                gxx = float(np.sum(bx * bx))
                gyy = float(np.sum(by * by))
                gxy = float(np.sum(bx * by))
                denom = gxx + gyy + 1e-6
                coh_x = gxx - gyy
                coh_y = 2.0 * gxy
                reliability = float(np.sqrt(coh_x * coh_x + coh_y * coh_y) / denom)
                angle = 0.5 * np.arctan2(coh_y, coh_x)
                row.append((float(np.cos(2.0 * angle)), float(np.sin(2.0 * angle))))
                weight_row.append(float(np.clip(reliability, 0.0, 1.0)))
            if row:
                rows.append(row)
                weights.append(weight_row)

        if not rows:
            return {
                "cos2": np.zeros((0, 0), dtype=np.float32),
                "sin2": np.zeros((0, 0), dtype=np.float32),
                "weight": np.zeros((0, 0), dtype=np.float32),
            }

        vectors = np.array(rows, dtype=np.float32)
        return {
            "cos2": vectors[:, :, 0],
            "sin2": vectors[:, :, 1],
            "weight": np.array(weights, dtype=np.float32),
        }

    def template_descriptor(self, img):
        return {
            "orientation": self.orientation_descriptor(img),
        }

    def normalized_correlation(self, a, b, mask):
        valid = mask > 0
        if int(np.sum(valid)) < 128:
            return 0.0

        av = a[valid].astype(np.float32)
        bv = b[valid].astype(np.float32)
        av = av - float(np.mean(av))
        bv = bv - float(np.mean(bv))
        denom = float(np.sqrt(np.sum(av * av) * np.sum(bv * bv))) + 1e-6
        return float(np.clip(np.sum(av * bv) / denom, -1.0, 1.0))

    def orientation_similarity(self, live_img, template_ridge, mask):
        live = self.orientation_descriptor(live_img)
        stored = template_ridge.get("orientation", {})
        if live["cos2"].shape != stored.get("cos2", np.zeros((0, 0))).shape:
            return 0.0

        h_blocks, w_blocks = live["cos2"].shape
        if h_blocks == 0 or w_blocks == 0:
            return 0.0

        mask_blocks = cv2.resize(
            mask.astype(np.float32),
            (w_blocks, h_blocks),
            interpolation=cv2.INTER_AREA,
        )
        weights = live["weight"] * stored["weight"] * np.clip(mask_blocks, 0.0, 1.0)
        total_weight = float(np.sum(weights))
        if total_weight < 0.5:
            return 0.0

        similarity = live["cos2"] * stored["cos2"] + live["sin2"] * stored["sin2"]
        return float(np.clip(np.sum(similarity * weights) / total_weight, -1.0, 1.0))

    def ridge_consistency(self, live_img, template, homography):
        stored_img = template["image"]
        h, w = stored_img.shape[:2]
        aligned = cv2.warpPerspective(
            live_img,
            homography,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid_mask = cv2.warpPerspective(
            np.ones(live_img.shape, dtype=np.uint8),
            homography,
            (w, h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        coverage = float(np.sum(valid_mask > 0)) / float(valid_mask.size)
        if coverage < 0.35:
            return {
                "ncc": 0.0,
                "edge_ncc": 0.0,
                "orientation": 0.0,
                "ridge_score": 0.0,
                "coverage": coverage,
            }

        ncc = self.normalized_correlation(aligned, stored_img, valid_mask)
        aligned_edges = cv2.Sobel(aligned, cv2.CV_32F, 1, 0, ksize=3)
        stored_edges = cv2.Sobel(stored_img, cv2.CV_32F, 1, 0, ksize=3)
        edge_ncc = self.normalized_correlation(aligned_edges, stored_edges, valid_mask)
        orientation = self.orientation_similarity(aligned, template["ridge"], valid_mask)
        ridge_score = (
            max(0.0, ncc) * 0.40 +
            max(0.0, edge_ncc) * 0.20 +
            max(0.0, orientation) * 0.40
        )

        return {
            "ncc": float(ncc),
            "edge_ncc": float(edge_ncc),
            "orientation": float(orientation),
            "ridge_score": float(ridge_score),
            "coverage": float(coverage),
        }
