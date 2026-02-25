"use strict";

const form = document.getElementById("ocr-form");
const pdfFileEl = document.getElementById("pdf_file");
const startButton = document.getElementById("start_button");
const cancelButton = document.getElementById("cancel_button");
const retryButton = document.getElementById("retry_button");
const apiKeyEl = document.getElementById("api_key");
const secretKeyEl = document.getElementById("secret_key");
const layoutEl = form.querySelector("select[name='layout']");
const languageTypeEl = form.querySelector("input[name='language_type']");

const statusPanel = document.getElementById("status_panel");
const statusTextEl = document.getElementById("status_text");
const progressFillEl = document.getElementById("progress_fill");
const progressValueEl = document.getElementById("progress_value");
const phaseLabelEl = document.getElementById("phase_label");
const pageStatusEl = document.getElementById("page_status");
const downloadLinkEl = document.getElementById("download_link");

let pollTimer = null;
let currentTaskId = "";

function resetStatus() {
  statusPanel.classList.remove("status-success", "status-failed");
  statusTextEl.textContent = "任务准备中";
  progressFillEl.style.width = "0%";
  progressValueEl.textContent = "0%";
  phaseLabelEl.textContent = "阶段：等待中";
  pageStatusEl.textContent = "读取页数中";
  downloadLinkEl.classList.add("is-hidden");
  downloadLinkEl.href = "#";
  cancelButton.classList.add("is-hidden");
  retryButton.classList.add("is-hidden");
  cancelButton.disabled = false;
  retryButton.disabled = false;
}

function phaseText(phase) {
  const map = {
    queued: "等待中",
    authenticating: "连接中",
    converting: "转换中",
    recognizing: "识别中",
    completed: "已完成",
    failed: "失败",
    retrying: "重试中",
    canceled: "已取消",
    completed_with_errors: "部分成功",
  };
  return map[phase] || "处理中";
}

function updateStatus(payload) {
  const progress = Math.max(0, Math.min(100, Number(payload.progress || 0)));
  const totalPages = Number(payload.pages_total || 0);
  const convertDone = Number(payload.convert_done || 0);
  const ocrDone = Number(payload.pages_done || 0);
  const retryTotal = Number(payload.retry_total || 0);
  const retryDone = Number(payload.retry_done || 0);
  const phase = payload.phase || "";

  progressFillEl.style.width = `${progress}%`;
  progressValueEl.textContent = `${progress}%`;
  statusTextEl.textContent = payload.message || "";
  phaseLabelEl.textContent = `阶段：${phaseText(phase)}`;

  if (totalPages <= 0) {
    if (phase === "retrying") {
      pageStatusEl.textContent = `重试 ${retryDone} / ${retryTotal || "?"} 页`;
    } else {
      pageStatusEl.textContent = "读取页数中";
    }
    return;
  }
  if (phase === "converting") {
    pageStatusEl.textContent = `转换 ${convertDone} / ${totalPages} 页`;
    return;
  }
  if (phase === "retrying") {
    pageStatusEl.textContent = `重试 ${retryDone} / ${retryTotal || "?"} 页`;
    return;
  }
  pageStatusEl.textContent = `识别 ${ocrDone} / ${totalPages} 页`;
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
  cancelButton.classList.toggle("is-hidden", !payload.can_cancel);
  retryButton.classList.toggle("is-hidden", !payload.can_retry);
  if (payload.download_url) {
    downloadLinkEl.classList.remove("is-hidden");
    downloadLinkEl.href = payload.download_url;
  } else {
    downloadLinkEl.classList.add("is-hidden");
    downloadLinkEl.href = "#";
  }

  if (payload.status === "completed" || payload.status === "completed_with_errors" || payload.status === "canceled") {
    stopPolling();
    startButton.disabled = false;
    cancelButton.classList.add("is-hidden");
    if (payload.status === "completed" || payload.status === "completed_with_errors") {
      statusPanel.classList.add("status-success");
    }
    if (payload.status === "canceled") {
      statusPanel.classList.add("status-failed");
    }
    return;
  }
  if (payload.status === "failed") {
    stopPolling();
    startButton.disabled = false;
    cancelButton.classList.add("is-hidden");
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
    currentTaskId = taskId;
    cancelButton.classList.remove("is-hidden");
    retryButton.classList.add("is-hidden");
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

cancelButton.addEventListener("click", async () => {
  if (!currentTaskId) {
    return;
  }
  cancelButton.disabled = true;
  try {
    const response = await fetch(`/api/cancel/${currentTaskId}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "取消任务失败");
    }
    statusTextEl.textContent = "正在取消任务...";
  } catch (error) {
    cancelButton.disabled = false;
    statusPanel.classList.add("status-failed");
    statusTextEl.textContent = error.message || "取消任务失败";
  }
});

retryButton.addEventListener("click", async () => {
  if (!currentTaskId) {
    return;
  }
  startButton.disabled = true;
  retryButton.disabled = true;
  cancelButton.classList.remove("is-hidden");
  cancelButton.disabled = false;
  statusPanel.classList.remove("status-success", "status-failed");

  try {
    const formData = new FormData();
    formData.append("api_key", apiKeyEl.value || "");
    formData.append("secret_key", secretKeyEl.value || "");
    formData.append("layout", layoutEl.value || "auto");
    formData.append("language_type", languageTypeEl.value || "CHN_ENG");

    const response = await fetch(`/api/retry/${currentTaskId}`, { method: "POST", body: formData });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "重试失败页启动失败");
    }

    await pollTask(currentTaskId);
    stopPolling();
    pollTimer = window.setInterval(() => {
      pollTask(currentTaskId).catch((error) => {
        stopPolling();
        startButton.disabled = false;
        retryButton.disabled = false;
        statusPanel.classList.add("status-failed");
        statusTextEl.textContent = error.message || "任务状态更新失败";
      });
    }, 1200);
  } catch (error) {
    startButton.disabled = false;
    retryButton.disabled = false;
    statusPanel.classList.add("status-failed");
    statusTextEl.textContent = error.message || "重试失败页启动失败";
  }
});
