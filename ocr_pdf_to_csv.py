#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate"
DEFAULT_CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / "baidu_ocr_credentials.txt"


class OCRRequestError(RuntimeError):
    pass


def sanitize_credential_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def parse_credentials_file(file_path: Path) -> tuple[str, str]:
    if not file_path.exists():
        raise OCRRequestError(f"credentials file not found: {file_path}")

    raw_lines = file_path.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")]

    api_key = ""
    secret_key = ""

    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        normalized_key = key.strip().lower().replace("-", "_").replace(" ", "_")
        normalized_value = sanitize_credential_value(value)
        if normalized_key in {"api_key", "apikey", "client_id", "ak", "access_key", "accesskey"} and normalized_value:
            api_key = normalized_value
        elif normalized_key in {"secret_key", "secretkey", "client_secret", "sk", "secret", "secretaccesskey"} and normalized_value:
            secret_key = normalized_value

    # Also support plain two-line format:
    # line1 -> API_KEY, line2 -> SECRET_KEY
    if not api_key and lines:
        first_line = lines[0]
        if "=" not in first_line and ":" not in first_line:
            api_key = first_line
    if not secret_key and len(lines) >= 2:
        second_line = lines[1]
        if "=" not in second_line and ":" not in second_line:
            secret_key = second_line

    return api_key, secret_key


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    api_key = args.api_key
    secret_key = args.secret_key

    cred_file: Path | None = args.credentials_file
    if cred_file is None and DEFAULT_CREDENTIALS_FILE.exists():
        cred_file = DEFAULT_CREDENTIALS_FILE

    if cred_file is not None:
        file_api_key, file_secret_key = parse_credentials_file(cred_file)
        if file_api_key:
            api_key = file_api_key
        if file_secret_key:
            secret_key = file_secret_key

    if not api_key:
        api_key = os.getenv("BAIDU_OCR_API_KEY", "")
    if not secret_key:
        secret_key = os.getenv("BAIDU_OCR_SECRET_KEY", "")

    return api_key, secret_key


def get_access_token(api_key: str, secret_key: str, timeout: float) -> str:
    params = {
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": secret_key,
    }
    try:
        response = requests.get(TOKEN_URL, params=params, timeout=timeout)
        payload = response.json()
    except requests.RequestException as exc:
        raise OCRRequestError(f"network error while requesting access_token: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise OCRRequestError("failed to parse token response as json") from exc

    if response.status_code >= 400:
        error = payload.get("error_description") or payload.get("error") or "http_error"
        raise OCRRequestError(f"failed to get access_token: status={response.status_code}, error={error}")
    token = payload.get("access_token")
    if not token:
        raise OCRRequestError(f"failed to get access_token: {payload}")
    return token


def convert_pdf_to_images(pdf_path: Path, images_dir: Path, dpi: int) -> list[Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    image_paths: list[Path] = []
    document = fitz.open(pdf_path)
    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_name = f"{pdf_path.stem}_page_{page_index + 1:04d}.png"
            image_path = images_dir / image_name
            pix.save(image_path)
            image_paths.append(image_path)
    finally:
        document.close()

    return image_paths


def ocr_image(
    image_path: Path,
    access_token: str,
    timeout: float,
    language_type: str,
) -> dict[str, Any]:
    image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data = {
        "image": image_base64,
        "language_type": language_type,
        "detect_direction": "true",
        "multidirectional_recognize": "true",
        "probability": "true",
    }
    try:
        response = requests.post(
            OCR_URL,
            params={"access_token": access_token},
            data=data,
            timeout=timeout,
        )
        payload = response.json()
    except requests.RequestException as exc:
        raise OCRRequestError(f"network error while OCR request for {image_path.name}: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise OCRRequestError(f"failed to parse OCR response as json for {image_path.name}") from exc

    if response.status_code >= 400:
        error = payload.get("error_msg") or payload.get("error_code") or "http_error"
        raise OCRRequestError(f"OCR request failed for {image_path.name}: status={response.status_code}, error={error}")
    if "error_code" in payload:
        raise OCRRequestError(f"OCR failed for {image_path.name}: {payload}")
    return payload


def choose_layout(words_result: list[dict[str, Any]], layout: str) -> str:
    if layout != "auto":
        return layout
    if len(words_result) < 2:
        return "horizontal"

    vertical_like = 0
    widths: list[float] = []
    band_ids: set[int] = set()

    for item in words_result:
        loc = item.get("location") or {}
        width = float(loc.get("width", 0) or 0)
        height = float(loc.get("height", 0) or 0)
        left = float(loc.get("left", 0) or 0)
        if width > 0:
            widths.append(width)
        if width > 0 and height > width * 1.15:
            vertical_like += 1

        band_size = 40.0
        if widths:
            band_size = max(20.0, statistics.median(widths) * 1.8)
        band_ids.add(int(left // band_size))

    vertical_ratio = vertical_like / len(words_result)
    if vertical_ratio >= 0.6 and len(band_ids) >= 2:
        return "vertical-rtl"
    return "horizontal"


def sort_words(words_result: list[dict[str, Any]], layout: str) -> list[dict[str, Any]]:
    valid_items = [
        item
        for item in words_result
        if isinstance(item, dict) and (item.get("words") or "").strip()
    ]
    if not valid_items:
        return []

    if layout == "vertical-rtl":
        widths = [
            float((item.get("location") or {}).get("width", 0) or 0)
            for item in valid_items
        ]
        median_width = statistics.median([w for w in widths if w > 0] or [20.0])
        col_step = max(12.0, median_width * 1.2)

        return sorted(
            valid_items,
            key=lambda item: (
                -int(float((item.get("location") or {}).get("left", 0) or 0) // col_step),
                float((item.get("location") or {}).get("top", 0) or 0),
                -float((item.get("location") or {}).get("left", 0) or 0),
            ),
        )

    heights = [
        float((item.get("location") or {}).get("height", 0) or 0) for item in valid_items
    ]
    median_height = statistics.median([h for h in heights if h > 0] or [20.0])
    row_step = max(8.0, median_height * 0.8)

    return sorted(
        valid_items,
        key=lambda item: (
            int(float((item.get("location") or {}).get("top", 0) or 0) // row_step),
            float((item.get("location") or {}).get("left", 0) or 0),
            float((item.get("location") or {}).get("top", 0) or 0),
        ),
    )


def line_confidence(item: dict[str, Any]) -> str:
    probability = item.get("probability")
    if isinstance(probability, dict):
        average = probability.get("average")
        if isinstance(average, (int, float)):
            return f"{average:.4f}"
    return ""


def build_rows(
    image_path: Path,
    page_no: int,
    words_result: list[dict[str, Any]],
    layout: str,
) -> list[dict[str, Any]]:
    sorted_items = sort_words(words_result, layout)
    rows: list[dict[str, Any]] = []

    for line_no, item in enumerate(sorted_items, start=1):
        loc = item.get("location") or {}
        rows.append(
            {
                "image_file": image_path.name,
                "page_no": page_no,
                "line_no": line_no,
                "layout": layout,
                "left": int(loc.get("left", 0) or 0),
                "top": int(loc.get("top", 0) or 0),
                "width": int(loc.get("width", 0) or 0),
                "height": int(loc.get("height", 0) or 0),
                "confidence": line_confidence(item),
                "text": (item.get("words") or "").strip(),
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baidu OCR PDF to CSV")
    parser.add_argument("pdf", type=Path, help="input PDF path")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output CSV path")
    parser.add_argument("--images-dir", type=Path, help="folder to store converted page images")
    parser.add_argument("--dpi", type=int, default=300, help="PDF render DPI (default: 300)")
    parser.add_argument(
        "--layout",
        choices=["auto", "horizontal", "vertical-rtl"],
        default="auto",
        help="line ordering mode (default: auto)",
    )
    parser.add_argument(
        "--language-type",
        default="CHN_ENG",
        help="Baidu OCR language_type parameter (default: CHN_ENG)",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help=(
            "txt file for API credentials. Supports KEY=VALUE or two-line format "
            "(line1 API_KEY, line2 SECRET_KEY). If omitted, auto-uses ../baidu_ocr_credentials.txt when it exists."
        ),
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Baidu OCR API Key (optional; fallback to credentials file or env BAIDU_OCR_API_KEY)",
    )
    parser.add_argument(
        "--secret-key",
        default="",
        help="Baidu OCR Secret Key (optional; fallback to credentials file or env BAIDU_OCR_SECRET_KEY)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout seconds (default: 60)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="sleep seconds between page requests (default: 0)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print processing progress to stderr",
    )
    return parser.parse_args()


def run() -> int:
    args = parse_args()

    if not args.pdf.exists():
        print(f"input PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    api_key, secret_key = resolve_credentials(args)
    if not api_key or not secret_key:
        print("missing api key/secret key", file=sys.stderr)
        return 1

    images_dir = args.images_dir or (args.output.parent / f"{args.pdf.stem}_images")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        token = get_access_token(api_key, secret_key, args.timeout)
        image_paths = convert_pdf_to_images(args.pdf, images_dir, args.dpi)
        rows: list[dict[str, Any]] = []

        for idx, image_path in enumerate(image_paths, start=1):
            if args.verbose:
                print(f"[{idx}/{len(image_paths)}] OCR {image_path.name}", file=sys.stderr)

            payload = ocr_image(image_path, token, args.timeout, args.language_type)
            words_result = payload.get("words_result") or []
            page_layout = choose_layout(words_result, args.layout)
            rows.extend(build_rows(image_path, idx, words_result, page_layout))

            if args.interval > 0:
                time.sleep(args.interval)

        with args.output.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "image_file",
                    "page_no",
                    "line_no",
                    "layout",
                    "left",
                    "top",
                    "width",
                    "height",
                    "confidence",
                    "text",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
