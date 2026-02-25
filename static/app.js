"use strict";

const form = document.getElementById("ocr-form");
const pdfFileEl = document.getElementById("pdf_file");
const startButton = document.getElementById("start_button");

const statusPanel = document.getElementById("status_panel");
const statusTextEl = document.getElementById("status_text");
const progressFillEl = document.getElementById("progress_fill");
const progressValueEl = document.getElementById("progress_value");
const pageStatusEl = document.getElementById("page_status");
const downloadLinkEl = document.getElementById("download_link");

let pollTimer = null;

function resetStatus() {
  statusPanel.classList.remove("status-success", "status-failed");
  statusTextEl.textContent = "任务准备中";
  progressFillEl.style.width = "0%";
  progressValueEl.textContent = "0%";
  pageStatusEl.textContent = "0 / 0 页";
  downloadLinkEl.classList.add("is-hidden");
  downloadLinkEl.href = "#";
}

function updateStatus(payload) {
  const progress = Math.max(0, Math.min(100, Number(payload.progress || 0)));
  progressFillEl.style.width = `${progress}%`;
  progressValueEl.textContent = `${progress}%`;
  statusTextEl.textContent = payload.message || "";
  pageStatusEl.textContent = `${payload.pages_done || 0} / ${payload.pages_total || 0} 页`;
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollTask(taskId) {
  const response = await fetch(`/api/status/${taskId}`);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "获取任务状态失败");
  }
  updateStatus(payload);

  if (payload.status === "completed") {
    stopPolling();
    startButton.disabled = false;
    statusPanel.classList.add("status-success");
    downloadLinkEl.classList.remove("is-hidden");
    downloadLinkEl.href = payload.download_url;
    return;
  }
  if (payload.status === "failed") {
    stopPolling();
    startButton.disabled = false;
    statusPanel.classList.add("status-failed");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  stopPolling();
  resetStatus();
  startButton.disabled = true;

  if (pdfFileEl.files.length === 0) {
    statusPanel.classList.add("status-failed");
    statusTextEl.textContent = "请先选择 PDF 文件";
    startButton.disabled = false;
    return;
  }

  try {
    const formData = new FormData(form);
    const response = await fetch("/api/start", { method: "POST", body: formData });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "任务启动失败");
    }

    const taskId = payload.task_id;
    await pollTask(taskId);
    pollTimer = window.setInterval(() => {
      pollTask(taskId).catch((error) => {
        stopPolling();
        startButton.disabled = false;
        statusPanel.classList.add("status-failed");
        statusTextEl.textContent = error.message || "任务状态更新失败";
      });
    }, 1200);
  } catch (error) {
    startButton.disabled = false;
    statusPanel.classList.add("status-failed");
    statusTextEl.textContent = error.message || "请求失败";
  }
});
