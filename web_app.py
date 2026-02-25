#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from ocr_pdf_to_csv import (
    OCRRequestError,
    build_rows,
    choose_layout,
    convert_pdf_to_images,
    get_access_token,
    ocr_image,
)

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "web_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()


def natural_key(text: str) -> list[Any]:
    parts = re.split(r"(\d+)", text)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def update_task(task_id: str, **updates: Any) -> None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        task.update(updates)
        task["updated_at"] = time.time()


def create_task(task_id: str, output_name: str) -> None:
    now = time.time()
    with TASKS_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "任务已创建",
            "pages_total": 0,
            "pages_done": 0,
            "rows_total": 0,
            "result_csv": "",
            "output_name": output_name,
            "created_at": now,
            "updated_at": now,
        }


def run_ocr_task(
    *,
    task_id: str,
    input_mode: str,
    pdf_path: Path | None,
    images_dir: Path,
    image_paths: list[Path],
    api_key: str,
    secret_key: str,
    layout: str,
    language_type: str,
    dpi: int,
) -> None:
    rows: list[dict[str, Any]] = []
    run_dir = RUNS_DIR / task_id
    result_csv = run_dir / f"{task_id}_ocr.csv"

    try:
        update_task(task_id, status="running", progress=3, message="正在连接百度 OCR 服务")
        access_token = get_access_token(api_key=api_key, secret_key=secret_key, timeout=60.0)

        if input_mode == "pdf":
            if not pdf_path:
                raise OCRRequestError("missing pdf input")
            update_task(task_id, progress=8, message="正在将 PDF 转换为图片")
            image_paths = convert_pdf_to_images(pdf_path=pdf_path, images_dir=images_dir, dpi=dpi)

        if not image_paths:
            raise OCRRequestError("未检测到可识别的图片")

        ordered_images = sorted(image_paths, key=lambda p: natural_key(p.name))
        total_pages = len(ordered_images)
        update_task(
            task_id,
            pages_total=total_pages,
            pages_done=0,
            progress=10,
            message=f"开始识别，共 {total_pages} 页",
        )

        for idx, image_path in enumerate(ordered_images, start=1):
            payload = ocr_image(
                image_path=image_path,
                access_token=access_token,
                timeout=60.0,
                language_type=language_type,
            )
            words_result = payload.get("words_result") or []
            page_layout = choose_layout(words_result=words_result, layout=layout)
            rows.extend(build_rows(image_path=image_path, page_no=idx, words_result=words_result, layout=page_layout))

            progress = min(98, int((idx / total_pages) * 88) + 10)
            update_task(
                task_id,
                pages_done=idx,
                progress=progress,
                message=f"正在识别第 {idx}/{total_pages} 页",
            )

        fieldnames = [
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
        ]
        with result_csv.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        update_task(
            task_id,
            status="completed",
            progress=100,
            rows_total=len(rows),
            result_csv=str(result_csv),
            message=f"识别完成，共 {len(rows)} 行文本",
        )
    except Exception as exc:
        error_message = str(exc).strip() or exc.__class__.__name__
        update_task(
            task_id,
            status="failed",
            message=f"任务失败：{error_message}",
        )


def save_pdf_upload(task_dir: Path) -> Path:
    file = request.files.get("pdf_file")
    if file is None or not file.filename:
        raise OCRRequestError("请上传 PDF 文件")

    filename = secure_filename(Path(file.filename).name) or "input.pdf"
    if not filename.lower().endswith(".pdf"):
        raise OCRRequestError("上传文件不是 PDF 格式")

    pdf_path = task_dir / filename
    file.save(pdf_path)
    return pdf_path


def save_image_uploads(task_dir: Path) -> list[Path]:
    files = request.files.getlist("image_files")
    if not files:
        raise OCRRequestError("请上传图片文件夹")

    saved: list[Path] = []
    for idx, file in enumerate(files, start=1):
        if not file.filename:
            continue
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_IMAGE_EXTS:
            continue
        base_name = secure_filename(Path(file.filename).name) or f"image_{idx}{suffix or '.png'}"
        dst = task_dir / f"{idx:04d}_{base_name}"
        file.save(dst)
        saved.append(dst)

    if not saved:
        raise OCRRequestError("未检测到有效图片，请上传 png/jpg/jpeg/tif 等格式")
    return saved


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/start")
def api_start() -> Any:
    try:
        input_mode = (request.form.get("input_mode") or "pdf").strip()
        if input_mode not in {"pdf", "images"}:
            raise OCRRequestError("input_mode 必须是 pdf 或 images")

        api_key = (request.form.get("api_key") or "").strip()
        secret_key = (request.form.get("secret_key") or "").strip()
        if not api_key or not secret_key:
            raise OCRRequestError("请填写 API_KEY 与 SECRET_KEY")

        layout = (request.form.get("layout") or "auto").strip()
        if layout not in {"auto", "horizontal", "vertical-rtl"}:
            raise OCRRequestError("layout 参数非法")

        language_type = (request.form.get("language_type") or "CHN_ENG").strip()
        dpi_raw = (request.form.get("dpi") or "300").strip()
        dpi = int(dpi_raw)
        if dpi < 72 or dpi > 600:
            raise OCRRequestError("dpi 范围建议在 72-600")

        task_id = uuid.uuid4().hex
        run_dir = RUNS_DIR / task_id
        input_dir = run_dir / "input"
        images_dir = run_dir / "images"
        input_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        pdf_path: Path | None = None
        image_paths: list[Path] = []
        output_name = "ocr_result.csv"

        if input_mode == "pdf":
            pdf_path = save_pdf_upload(input_dir)
            output_name = f"{pdf_path.stem}_ocr.csv"
        else:
            image_paths = save_image_uploads(input_dir)
            output_name = "images_ocr.csv"

        create_task(task_id=task_id, output_name=output_name)
        thread = threading.Thread(
            target=run_ocr_task,
            kwargs={
                "task_id": task_id,
                "input_mode": input_mode,
                "pdf_path": pdf_path,
                "images_dir": images_dir,
                "image_paths": image_paths,
                "api_key": api_key,
                "secret_key": secret_key,
                "layout": layout,
                "language_type": language_type,
                "dpi": dpi,
            },
            daemon=True,
        )
        thread.start()

        return jsonify({"task_id": task_id})
    except OCRRequestError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError:
        return jsonify({"error": "dpi 必须是整数"}), 400
    except Exception:
        return jsonify({"error": "请求处理失败"}), 500


@app.get("/api/status/<task_id>")
def api_status(task_id: str) -> Any:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        payload = {
            "task_id": task["task_id"],
            "status": task["status"],
            "progress": task["progress"],
            "message": task["message"],
            "pages_total": task["pages_total"],
            "pages_done": task["pages_done"],
            "rows_total": task["rows_total"],
            "download_url": f"/api/download/{task_id}" if task["status"] == "completed" else "",
        }
    return jsonify(payload)


@app.get("/api/download/<task_id>")
def api_download(task_id: str) -> Any:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task["status"] != "completed":
            return jsonify({"error": "task not completed"}), 400
        csv_path = Path(task["result_csv"])
        output_name = task.get("output_name") or f"{task_id}_ocr.csv"

    if not csv_path.exists():
        return jsonify({"error": "csv file missing"}), 404

    return send_file(
        csv_path,
        as_attachment=True,
        download_name=output_name,
        mimetype="text/csv",
    )


if __name__ == "__main__":
    host = os.getenv("OCR_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("OCR_WEB_PORT", "7860"))

    if os.getenv("OCR_WEB_AUTO_OPEN", "0") == "1":
        url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{url_host}:{port}"

        def _open_browser() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(url, new=2)
            except Exception:
                pass

        threading.Thread(target=_open_browser, daemon=True).start()

    app.run(host=host, port=port, debug=False)
