#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from ocr_pdf_to_csv import OCRRequestError, build_rows, choose_layout, get_access_token, ocr_image

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "web_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()
CSV_FIELDNAMES = [
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


class TaskCancelledError(RuntimeError):
    pass


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
            "phase": "queued",
            "progress": 0,
            "message": "任务已创建",
            "pages_total": 0,
            "convert_done": 0,
            "pages_done": 0,
            "retry_total": 0,
            "retry_done": 0,
            "rows_total": 0,
            "result_csv": "",
            "output_name": output_name,
            "failed_pages": [],
            "image_paths": [],
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
        }


def is_cancel_requested(task_id: str) -> bool:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        return bool(task and task.get("cancel_requested"))


def get_task_snapshot(task_id: str) -> dict[str, Any] | None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        return dict(task)


def write_rows_to_csv(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def read_rows_from_csv(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return [dict(row) for row in reader]


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def recognize_single_page(
    image_path: Path,
    page_no: int,
    access_token: str,
    layout: str,
    language_type: str,
) -> list[dict[str, Any]]:
    payload = ocr_image(
        image_path=image_path,
        access_token=access_token,
        timeout=60.0,
        language_type=language_type,
    )
    words_result = payload.get("words_result") or []
    page_layout = choose_layout(words_result=words_result, layout=layout)
    return build_rows(image_path=image_path, page_no=page_no, words_result=words_result, layout=page_layout)


def convert_pdf_to_images_with_progress(
    pdf_path: Path,
    images_dir: Path,
    task_id: str,
    dpi: int,
) -> list[Path]:
    image_paths: list[Path] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    document = fitz.open(pdf_path)
    try:
        total_pages = document.page_count
        if total_pages <= 0:
            raise OCRRequestError("PDF 没有可识别页面")

        update_task(
            task_id,
            phase="converting",
            pages_total=total_pages,
            convert_done=0,
            pages_done=0,
            progress=5,
            message=f"正在将 PDF 转换为图片 0/{total_pages}",
        )

        for idx in range(1, total_pages + 1):
            if is_cancel_requested(task_id):
                raise TaskCancelledError("任务已取消")
            page = document.load_page(idx - 1)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_name = f"{pdf_path.stem}_page_{idx:04d}.png"
            image_path = images_dir / image_name
            pix.save(image_path)
            image_paths.append(image_path)

            convert_progress = min(45, 5 + int((idx / total_pages) * 40))
            update_task(
                task_id,
                phase="converting",
                convert_done=idx,
                progress=convert_progress,
                message=f"正在将 PDF 转换为图片 {idx}/{total_pages}",
            )
    finally:
        document.close()

    return image_paths


def run_retry_task(
    *,
    task_id: str,
    api_key: str,
    secret_key: str,
    layout: str,
    language_type: str,
) -> None:
    snapshot = get_task_snapshot(task_id)
    if not snapshot:
        return

    failed_pages = [parse_int(p, 0) for p in (snapshot.get("failed_pages") or []) if parse_int(p, 0) > 0]
    image_paths_raw = snapshot.get("image_paths") or []
    image_paths = [Path(path) for path in image_paths_raw]
    result_csv = Path(snapshot.get("result_csv") or "")
    if not failed_pages or not image_paths or not result_csv:
        update_task(task_id, message="没有可重试的失败页", phase="failed")
        return

    update_task(
        task_id,
        status="running",
        phase="retrying",
        cancel_requested=False,
        retry_total=len(failed_pages),
        retry_done=0,
        progress=5,
        message=f"正在重试失败页 0/{len(failed_pages)}",
    )

    try:
        access_token = get_access_token(api_key=api_key, secret_key=secret_key, timeout=60.0)
        existing_rows = read_rows_from_csv(result_csv)
        failed_set = set(failed_pages)
        kept_rows = [row for row in existing_rows if parse_int(row.get("page_no"), 0) not in failed_set]

        retried_rows: list[dict[str, Any]] = []
        remaining_failed: list[int] = []

        total_retry = len(failed_pages)
        for idx, page_no in enumerate(failed_pages, start=1):
            if is_cancel_requested(task_id):
                raise TaskCancelledError("任务已取消")

            progress = min(98, 10 + int((idx / total_retry) * 88))
            update_task(
                task_id,
                phase="retrying",
                retry_done=idx - 1,
                progress=progress,
                message=f"正在重试失败页 {idx}/{total_retry}",
            )

            if page_no < 1 or page_no > len(image_paths):
                remaining_failed.append(page_no)
                continue

            image_path = image_paths[page_no - 1]
            try:
                retried_rows.extend(
                    recognize_single_page(
                        image_path=image_path,
                        page_no=page_no,
                        access_token=access_token,
                        layout=layout,
                        language_type=language_type,
                    )
                )
            except Exception:
                remaining_failed.append(page_no)

            update_task(
                task_id,
                phase="retrying",
                retry_done=idx,
                progress=progress,
                message=f"正在重试失败页 {idx}/{total_retry}",
            )

        merged_rows = kept_rows + retried_rows
        merged_rows.sort(key=lambda row: (parse_int(row.get("page_no")), parse_int(row.get("line_no"))))
        write_rows_to_csv(result_csv, merged_rows)

        if remaining_failed:
            update_task(
                task_id,
                status="completed_with_errors",
                phase="completed_with_errors",
                progress=100,
                failed_pages=remaining_failed,
                rows_total=len(merged_rows),
                message=f"重试结束，仍有 {len(remaining_failed)} 页失败",
            )
        else:
            update_task(
                task_id,
                status="completed",
                phase="completed",
                progress=100,
                failed_pages=[],
                rows_total=len(merged_rows),
                message="重试完成，所有失败页已成功识别",
            )
    except TaskCancelledError as exc:
        update_task(task_id, status="canceled", phase="canceled", message=str(exc))
    except Exception as exc:
        update_task(
            task_id,
            status="failed",
            phase="failed",
            message=f"重试失败：{str(exc).strip() or exc.__class__.__name__}",
        )


def run_ocr_task(
    *,
    task_id: str,
    pdf_path: Path,
    images_dir: Path,
    api_key: str,
    secret_key: str,
    layout: str,
    language_type: str,
    dpi: int,
) -> None:
    rows: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    run_dir = RUNS_DIR / task_id
    result_csv = run_dir / f"{task_id}_ocr.csv"

    try:
        update_task(
            task_id,
            status="running",
            phase="authenticating",
            progress=3,
            message="正在连接百度 OCR 服务",
        )
        access_token = get_access_token(api_key=api_key, secret_key=secret_key, timeout=60.0)
        image_paths = convert_pdf_to_images_with_progress(
            pdf_path=pdf_path,
            images_dir=images_dir,
            task_id=task_id,
            dpi=dpi,
        )
        update_task(task_id, image_paths=[str(path) for path in image_paths], result_csv=str(result_csv))

        total_pages = len(image_paths)
        if total_pages <= 0:
            raise OCRRequestError("未检测到可识别页面")

        for idx, image_path in enumerate(image_paths, start=1):
            if is_cancel_requested(task_id):
                raise TaskCancelledError("任务已取消")
            ocr_progress = min(98, 45 + int((idx / total_pages) * 53))
            update_task(
                task_id,
                phase="recognizing",
                progress=ocr_progress,
                message=f"正在识别第 {idx}/{total_pages} 页",
            )

            try:
                rows.extend(
                    recognize_single_page(
                        image_path=image_path,
                        page_no=idx,
                        access_token=access_token,
                        layout=layout,
                        language_type=language_type,
                    )
                )
            except Exception:
                failed_pages.append(idx)

            update_task(
                task_id,
                phase="recognizing",
                pages_done=idx,
                progress=ocr_progress,
                message=f"正在识别第 {idx}/{total_pages} 页",
            )

        write_rows_to_csv(result_csv, rows)

        if failed_pages:
            update_task(
                task_id,
                status="completed_with_errors",
                phase="completed_with_errors",
                progress=100,
                convert_done=total_pages,
                pages_done=total_pages,
                rows_total=len(rows),
                result_csv=str(result_csv),
                failed_pages=failed_pages,
                message=f"识别完成，失败 {len(failed_pages)} 页，可重试失败页",
            )
        else:
            update_task(
                task_id,
                status="completed",
                phase="completed",
                progress=100,
                convert_done=total_pages,
                pages_done=total_pages,
                rows_total=len(rows),
                result_csv=str(result_csv),
                failed_pages=[],
                message=f"识别完成，共 {len(rows)} 行文本",
            )
    except TaskCancelledError as exc:
        if rows:
            write_rows_to_csv(result_csv, rows)
            update_task(task_id, result_csv=str(result_csv), rows_total=len(rows))
        update_task(task_id, status="canceled", phase="canceled", message=str(exc))
    except Exception as exc:
        error_message = str(exc).strip() or exc.__class__.__name__
        update_task(
            task_id,
            status="failed",
            phase="failed",
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


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/start")
def api_start() -> Any:
    try:
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

        pdf_path = save_pdf_upload(input_dir)
        output_name = f"{pdf_path.stem}_ocr.csv"

        create_task(task_id=task_id, output_name=output_name)
        thread = threading.Thread(
            target=run_ocr_task,
            kwargs={
                "task_id": task_id,
                "pdf_path": pdf_path,
                "images_dir": images_dir,
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


@app.post("/api/cancel/<task_id>")
def api_cancel(task_id: str) -> Any:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task["status"] not in {"queued", "running"}:
            return jsonify({"error": "task not cancelable"}), 400
        task["cancel_requested"] = True
        task["message"] = "正在取消任务..."
        task["updated_at"] = time.time()
    return jsonify({"ok": True})


@app.post("/api/retry/<task_id>")
def api_retry(task_id: str) -> Any:
    api_key = (request.form.get("api_key") or "").strip()
    secret_key = (request.form.get("secret_key") or "").strip()
    layout = (request.form.get("layout") or "auto").strip()
    language_type = (request.form.get("language_type") or "CHN_ENG").strip()
    if not api_key or not secret_key:
        return jsonify({"error": "重试需要 API_KEY 与 SECRET_KEY"}), 400
    if layout not in {"auto", "horizontal", "vertical-rtl"}:
        return jsonify({"error": "layout 参数非法"}), 400

    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task["status"] in {"queued", "running"}:
            return jsonify({"error": "任务正在运行，无法重试"}), 400
        if not task.get("failed_pages"):
            return jsonify({"error": "当前任务没有失败页可重试"}), 400

    thread = threading.Thread(
        target=run_retry_task,
        kwargs={
            "task_id": task_id,
            "api_key": api_key,
            "secret_key": secret_key,
            "layout": layout,
            "language_type": language_type,
        },
        daemon=True,
    )
    thread.start()
    return jsonify({"task_id": task_id})


@app.get("/api/status/<task_id>")
def api_status(task_id: str) -> Any:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        payload = {
            "task_id": task["task_id"],
            "status": task["status"],
            "phase": task.get("phase", ""),
            "progress": task["progress"],
            "message": task["message"],
            "pages_total": task["pages_total"],
            "convert_done": task.get("convert_done", 0),
            "pages_done": task["pages_done"],
            "retry_total": task.get("retry_total", 0),
            "retry_done": task.get("retry_done", 0),
            "rows_total": task["rows_total"],
            "failed_pages_count": len(task.get("failed_pages", [])),
            "can_cancel": task["status"] in {"queued", "running"},
            "can_retry": task["status"] in {"completed_with_errors", "failed", "canceled"} and len(task.get("failed_pages", [])) > 0,
            "download_url": f"/api/download/{task_id}" if task["status"] in {"completed", "completed_with_errors", "canceled"} else "",
        }
    return jsonify(payload)


@app.get("/api/download/<task_id>")
def api_download(task_id: str) -> Any:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task["status"] not in {"completed", "completed_with_errors", "canceled"}:
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
