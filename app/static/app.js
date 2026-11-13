/* HelloMinerU 工作台前端：原生 JS，三视图（解析 / 评测 / 节点）。 */
(() => {
  "use strict";

  const SCORE_FIELDS = [
    { key: "text_accuracy", label: "文本准确" },
    { key: "reading_order", label: "阅读顺序" },
    { key: "table", label: "表格" },
    { key: "formula", label: "公式" },
    { key: "image", label: "图片图表" },
  ];
  const ANNOTATION_LABELS = ["文本错误", "表格错误", "公式错误", "顺序错误", "图片错误", "漏检", "多余"];

  const state = {
    parsers: [],
    groups: [],
    jobs: [],
    selected: null,
    job: null,
    result: null,
    logOffset: 0,
    annotator: localStorage.getItem("hmu.annotator") || "tester",
    documents: [],
    compare: { left: null, right: null },
    busy: false,
    tick: 0,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);

  let toastTimer = null;
  function toast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = isError ? "toast error" : "toast";
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3800);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
    if (!response.ok) {
      let detail = payload && payload.detail ? payload.detail : payload;
      if (Array.isArray(detail)) {
        detail = detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
      } else if (detail && typeof detail === "object") {
        detail = detail.message || JSON.stringify(detail);
      }
      const error = new Error(detail || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  const fmtTime = (value) => {
    if (!value) return "—";
    const date = new Date(value.endsWith("Z") || value.includes("+") ? value : value + "Z");
    return date.toLocaleString("zh-CN", { hour12: false });
  };
  const fmtDuration = (ms) => {
    if (ms === null || ms === undefined) return "—";
    return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
  };
  const fmtSize = (bytes) => {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  };
  const statusChip = (status) => `<span class="badge ${esc(status)}">${esc(status)}</span>`;

  function renderMarkdown(target, markdown) {
    const html = marked.parse(markdown || "", { gfm: true, breaks: true });
    target.innerHTML = DOMPurify.sanitize(html);
    if (window.renderMathInElement) {
      renderMathInElement(target, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
      });
    }
    target.querySelectorAll("pre code").forEach((block) => {
      if (window.hljs) { try { hljs.highlightElement(block); } catch { /* ignore */ } }
    });
  }

  function fileUrl(jobId, relPath) {
    return `/api/jobs/${encodeURIComponent(jobId)}/files/output/${relPath
      .split("/").map(encodeURIComponent).join("/")}`;
  }

  /* ------------------------------------------------------------ navigation */
  function showView(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.view === name);
    });
    document.querySelectorAll(".view").forEach((view) => {
      view.classList.toggle("active", view.id === `view-${name}`);
    });
    if (name === "evaluation") loadEvaluation();
    if (name === "workers") loadWorkers();
  }

  function showResultTab(name) {
    document.querySelectorAll(".subtab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.tab === name);
    });
    document.querySelectorAll(".subview").forEach((view) => {
      view.classList.toggle("active", view.id === `tab-${name}`);
    });
    if (name === "log" && state.selected) loadLog(false);
  }

  /* -------------------------------------------------------------- metadata */
  async function loadParsers() {
    const data = await api("/api/parsers");
    state.parsers = data.parsers || [];
    state.groups = data.option_groups || [];
    const select = $("engine-select");
    select.innerHTML = state.parsers
      .map((parser) => `<option value="${esc(parser.id)}">${esc(parser.label)}</option>`)
      .join("");
    select.value = "auto";
    renderOptionForm();
    updateEngineHint();
  }

  function renderOptionForm() {
    const container = $("option-groups");
    const blocks = [];
    for (const group of state.groups) {
      if (group.id === "common") {
        // engine 由独立选择器控制；common 中其余字段仍展示
      }
      const fields = [];
      for (const field of group.fields || []) {
        if (field.name === "engine") continue;
        const id = `opt-${field.name.replace(/\./g, "-")}`;
        let input = "";
        if (field.type === "select") {
          input = `<select id="${id}" data-name="${esc(field.name)}">${(field.choices || [])
            .map((choice) => `<option value="${esc(choice.value)}"${
              String(choice.value) === String(field.default) ? " selected" : ""
            }>${esc(choice.label)}</option>`).join("")}</select>`;
        } else if (field.type === "boolean") {
          input = `<label class="checkbox"><input type="checkbox" id="${id}" data-name="${esc(field.name)}"${
            field.default ? " checked" : ""
          } /> 启用</label>`;
        } else if (field.type === "number") {
          input = `<input type="number" id="${id}" data-name="${esc(field.name)}"
            value="${field.default === null || field.default === undefined ? "" : esc(field.default)}"
            ${field.min !== undefined ? `min="${field.min}"` : ""}
            ${field.max !== undefined ? `max="${field.max}"` : ""} placeholder="默认" />`;
        } else {
          input = `<input type="text" id="${id}" data-name="${esc(field.name)}"
            value="${field.default === null || field.default === undefined ? "" : esc(field.default)}" />`;
        }
        fields.push(`<div class="field" data-group="${esc(group.id)}">
          <label for="${id}">${esc(field.label)}</label>${input}
          ${field.help ? `<span class="help">${esc(field.help)}</span>` : ""}
        </div>`);
      }
      if (fields.length) {
        blocks.push(`<div class="group" data-group="${esc(group.id)}">
          <h4>${esc(group.label)}</h4>${fields.join("")}</div>`);
      }
    }
    container.innerHTML = blocks.join("");
    container.querySelectorAll("input, select").forEach((input) => {
      input.addEventListener("change", updateOptionVisibility);
    });
    updateOptionVisibility();
  }

  function updateOptionVisibility() {
    const engine = $("engine-select").value;
    const showMineru = engine === "auto" || engine === "mineru";
    const showPymupdf = engine === "auto" || engine === "pymupdf";
    document.querySelectorAll('#option-groups .group[data-group="mineru"]').forEach((node) => {
      node.classList.toggle("hidden", !showMineru);
    });
    document.querySelectorAll('#option-groups .group[data-group="pymupdf"]').forEach((node) => {
      node.classList.toggle("hidden", !showPymupdf);
    });
  }

  function updateEngineHint() {
    const engine = $("engine-select").value;
    const parser = state.parsers.find((item) => item.id === engine);
    $("engine-hint").textContent = parser ? parser.description : "";
    updateOptionVisibility();
  }

  function buildOptions() {
    const options = { engine: $("engine-select").value, mineru: {}, pymupdf: {} };
    document.querySelectorAll("#option-groups [data-name]").forEach((input) => {
      const name = input.dataset.name;
      let value;
      if (input.type === "checkbox") value = input.checked;
      else if (input.type === "number") value = input.value === "" ? null : Number(input.value);
      else value = input.value;
      if (value === "") value = null;
      const [head, tail] = name.split(".");
      if (tail) options[head][tail] = value;
      else options[head] = value;
    });
    return options;
  }

  /* ------------------------------------------------------------- file input */
  async function handleFile(file) {
    if (!file) return;
    $("file-info").hidden = false;
    $("file-name").textContent = file.name;
    $("file-size").textContent = fmtSize(file.size);
    $("detect-box").innerHTML = `<span class="muted">正在识别文档类型…</span>`;
    $("submit-btn").disabled = true;
    const form = new FormData();
    form.append("file", file);
    try {
      const detection = await api("/api/detect", { method: "POST", body: form });
      state.detection = detection;
      const recommended = detection.recommended_engine || "mineru";
      const reasons = (detection.reasons || []).map((item) => `<li>${esc(item)}</li>`).join("");
      $("detect-box").innerHTML = `
        <div><span class="badge engine">${esc(detection.kind || "unknown")}</span>
        推荐引擎 <strong>${esc(recommended)}</strong>
        ${detection.pdf ? `· ${detection.pdf.page_count} 页 · 平均 ${detection.pdf.avg_chars_per_page} 字符/页` : ""}
        ${detection.pdf && detection.pdf.recommended_mineru_method ? `· 建议 method=${esc(detection.pdf.recommended_mineru_method)}` : ""}
        </div>
        ${reasons ? `<ul style="margin:6px 0 0;padding-left:18px">${reasons}</ul>` : ""}`;
      const engineSelect = $("engine-select");
      if (["pymupdf", "docx", "pptx", "xlsx"].includes(recommended)) engineSelect.value = recommended;
      else engineSelect.value = "auto";
      updateEngineHint();
      $("submit-btn").disabled = false;
    } catch (error) {
      $("detect-box").innerHTML = `<span class="badge failed">识别失败</span> ${esc(error.message)}`;
    }
  }

  async function submitJob() {
    if (!state.file) { toast("请先选择文件", true); return; }
    const button = $("submit-btn");
    button.disabled = true;
    button.textContent = "提交中…";
    const form = new FormData();
    form.append("file", state.file);
    form.append("options", JSON.stringify(buildOptions()));
    try {
      const job = await api("/api/parse", { method: "POST", body: form });
      toast(`任务已提交：${job.job_id}（${job.engine}）`);
      await refreshJobs();
      await selectJob(job.job_id);
      state.file = null;
      $("file-input").value = "";
      $("file-info").hidden = true;
      $("submit-btn").disabled = true;
      button.textContent = "开始解析";
    } catch (error) {
      toast(`提交失败：${error.message}`, true);
      button.textContent = "开始解析";
      button.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ jobs */
  async function refreshJobs() {
    try {
      const data = await api("/api/jobs?limit=50");
      state.jobs = data.jobs || [];
      renderJobs();
    } catch (error) {
      $("jobs-summary").textContent = `加载失败：${error.message}`;
    }
  }

  function renderJobs() {
    const tbody = $("jobs-table").querySelector("tbody");
    const jobs = state.jobs;
    $("jobs-empty").hidden = jobs.length > 0;
    const active = jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
    $("jobs-summary").textContent = `共 ${jobs.length} 条 · 进行中 ${active}`;
    tbody.innerHTML = jobs.map((job) => {
      const score = job.score_avg === null || job.score_avg === undefined
        ? "—" : `${Number(job.score_avg).toFixed(2)} (${job.score_count})`;
      const actions = ["queued", "running"].includes(job.status)
        ? `<button class="ghost small" data-cancel="${esc(job.id)}">取消</button>`
        : `<button class="ghost small danger" data-delete="${esc(job.id)}">删除</button>`;
      return `<tr data-job="${esc(job.id)}" class="${job.id === state.selected ? "selected" : ""}">
        <td><code>${esc(job.id.slice(0, 8))}</code></td>
        <td title="${esc(job.filename)}">${esc(job.filename)}</td>
        <td><span class="badge engine">${esc(job.engine)}</span></td>
        <td>${statusChip(job.status)}</td>
        <td>${esc(score)}</td>
        <td>${fmtDuration(job.duration_ms)}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr[data-job]").forEach((row) => {
      row.addEventListener("click", () => selectJob(row.dataset.job));
    });
    tbody.querySelectorAll("[data-cancel]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        await cancelJob(button.dataset.cancel);
      });
    });
    tbody.querySelectorAll("[data-delete]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        await deleteJob(button.dataset.delete);
      });
    });
  }

  async function selectJob(jobId) {
    if (jobId === state.selected && state.result) return;
    state.selected = jobId;
    state.job = null;
    state.result = null;
    state.lastStatus = null;
    state.logOffset = 0;
    renderJobs();
    $("result-panel").hidden = false;
    $("cancel-job").hidden = true;
    $("delete-job").hidden = true;
    $("result-title").textContent = "加载中…";
    $("result-subtitle").textContent = jobId;
    resetResultViews("正在加载任务…");
    await loadJobDetail(jobId, true);
    await loadResult(jobId);
    await loadLog(true);
    showResultTab("preview");
  }

  function resetResultViews(message) {
    $("markdown-rendered").innerHTML = `<p class="empty">${esc(message)}</p>`;
    $("markdown-raw").textContent = "";
    $("pages-grid").innerHTML = "";
    $("pages-empty").hidden = false;
    $("blocks-list").innerHTML = "";
    $("blocks-empty").hidden = false;
    $("artifacts").innerHTML = "";
    $("artifacts-empty").hidden = false;
    $("log-box").textContent = "";
    $("item-list").innerHTML = "";
    $("items-empty").hidden = false;
    $("annotation-meta").textContent = "";
    renderAnnotation(null, []);
  }

  async function loadJobDetail(jobId, initial = false) {
    try {
      const detail = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (state.selected !== jobId) return;
      const job = detail.job;
      state.job = job;
      $("result-title").textContent = job.filename;
      const queue = detail.queue_position ? ` · 队列第 ${detail.queue_position} 位` : "";
      $("result-subtitle").innerHTML = `${statusChip(job.status)}
        <span class="badge engine">${esc(job.engine)}</span>
        ${job.stage ? `<span class="muted">stage=${esc(job.stage)}</span>` : ""}
        ${job.error ? `<span class="badge failed" title="${esc(job.error)}">错误</span>` : ""}${queue}
        <span class="muted">· ${fmtTime(job.created_at)} · ${fmtDuration(job.duration_ms)}</span>`;
      $("cancel-job").hidden = !["queued", "running"].includes(job.status);
      $("delete-job").hidden = job.status === "running";
      if (initial && job.error) {
        $("markdown-rendered").innerHTML =
          `<p class="empty"><span class="badge failed">失败</span> ${esc(job.error)}</p>`;
      }
      return job;
    } catch (error) {
      $("result-title").textContent = "任务加载失败";
      $("result-subtitle").textContent = error.message;
    }
    return null;
  }

  async function loadResult(jobId) {
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/result`);
      if (state.selected !== jobId) return;
      state.result = result;
      renderResult(result);
    } catch (error) {
      if (state.selected !== jobId) return;
      const job = state.job;
      const message = job && job.error
        ? `解析失败：${job.error}`
        : `结果尚未生成（${job ? job.status : error.status || "未知"}）`;
      $("markdown-rendered").innerHTML = `<p class="empty">${esc(message)}</p>`;
      $("markdown-raw").textContent = "";
      $("markdown-stats").textContent = "";
      $("pages-empty").hidden = false;
      $("blocks-empty").hidden = false;
      $("artifacts-empty").hidden = false;
    }
  }

  function renderResult(result) {
    const markdown = result.markdown || "";
    $("markdown-rendered").innerHTML = "";
    if (markdown) {
      renderMarkdown($("markdown-rendered"), markdown);
    } else {
      $("markdown-rendered").innerHTML = `<p class="empty">Markdown 为空</p>`;
    }
    $("markdown-raw").textContent = markdown;
    $("markdown-stats").textContent = `${markdown.length} 字符 · ${result.content_list.length} 个内容块`;
    $("markdown-raw").hidden = true;
    $("toggle-raw").textContent = "查看原始 Markdown";
    $("markdown-rendered").hidden = false;

    // 页面缩略图
    const pages = result.pages || [];
    $("pages-empty").hidden = pages.length > 0;
    $("pages-grid").innerHTML = pages.map((page) => `
      <div class="page-card">
        <a href="${esc(page.url)}" target="_blank" rel="noreferrer">
          <img src="${esc(page.url)}" alt="page ${page.page_idx + 1}" loading="lazy" />
        </a>
        <div class="page-meta"><span>第 ${page.page_idx + 1} 页</span>
        <span>${page.width || "?"} × ${page.height || "?"}</span></div>
      </div>`).join("");

    renderBlocks(result);
    renderArtifacts(result);
    renderAnnotation(result.annotation, result.annotation_items || []);
  }

  function renderBlocks(result) {
    const items = result.content_list || [];
    $("blocks-empty").hidden = items.length > 0;
    $("blocks-list").innerHTML = items.map((item, index) => {
      const ref = `p${item.page_idx ?? 0}:b${index}`;
      const type = item.type || "unknown";
      let body = "";
      if (type === "table") {
        body = `<div class="md-frag">${DOMPurify.sanitize(marked.parse(item.table_body || "", { gfm: true }))}</div>`;
      } else if (type === "image") {
        const path = item.img_path || item.image_path || "";
        body = path
          ? `<img src="${esc(fileUrl(state.selected, path))}" alt="${esc(path)}" loading="lazy" />`
          : `<span class="muted">图片路径缺失</span>`;
      } else if (type === "equation") {
        body = `<div>$$${esc(item.text || item.latex || "")}$$</div>`;
      } else {
        body = esc(item.text || "");
      }
      return `<div class="block" data-index="${index}">
        <div class="block-head">
          <span class="badge">${esc(type)}</span>
          <span class="muted tiny">page ${(item.page_idx ?? 0) + 1} · ${esc(ref)}</span>
          <button class="ghost small mark-btn" data-mark="${esc(ref)}" data-page="${item.page_idx ?? 0}">标注</button>
        </div>
        <div class="block-body">${body}</div>
      </div>`;
    }).join("");

    // 渲染内容块中的公式
    if (window.renderMathInElement) {
      renderMathInElement($("blocks-list"), {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
      });
    }
    $("blocks-list").querySelectorAll("[data-mark]").forEach((button) => {
      button.addEventListener("click", () => {
        $("item-block").value = button.dataset.mark;
        $("item-block").dataset.page = button.dataset.page;
        showResultTab("annotation");
        $("item-label").focus();
      });
    });
  }

  function renderArtifacts(result) {
    const artifacts = result.artifacts || [];
    $("artifacts-empty").hidden = artifacts.length > 0;
    $("artifacts").innerHTML = artifacts.map((artifact) => `
      <div class="artifact">
        <span class="badge">${esc(artifact.kind || "file")}</span>
        <a class="name" href="${esc(artifact.url)}" target="_blank" rel="noreferrer">${esc(artifact.name)}</a>
        <span class="meta">${esc(artifact.rel)} · ${fmtSize(artifact.size)}</span>
      </div>`).join("");
  }

  /* ------------------------------------------------------------ annotation */
  function initAnnotationForm() {
    $("score-fields").innerHTML = SCORE_FIELDS.map((field) => `
      <div class="score-row">
        <label for="score-${field.key}">${field.label}</label>
        <input type="range" id="score-${field.key}" min="1" max="5" step="1" value="3" />
        <span class="val" id="val-${field.key}">3</span>
      </div>`).join("");
    SCORE_FIELDS.forEach((field) => {
      $("score-" + field.key).addEventListener("input", (event) => {
        $("val-" + field.key).textContent = event.target.value;
      });
    });
    $("item-label").innerHTML = ANNOTATION_LABELS
      .map((label) => `<option value="${esc(label)}">${esc(label)}</option>`).join("");
    $("annotator").value = state.annotator;
    $("annotator").addEventListener("change", () => {
      state.annotator = $("annotator").value.trim() || "tester";
      localStorage.setItem("hmu.annotator", state.annotator);
      if (state.selected) loadAnnotationForCurrent();
    });
  }

  function renderAnnotation(annotation, items) {
    const scores = (annotation && annotation.scores) || {};
    SCORE_FIELDS.forEach((field) => {
      const input = $("score-" + field.key);
      const value = scores[field.key] ?? 3;
      input.value = value;
      $("val-" + field.key).textContent = value;
    });
    $("annotation-comment").value = (annotation && annotation.comment) || "";
    $("annotation-status").value = (annotation && annotation.status) || "draft";
    $("annotation-meta").textContent = annotation
      ? `最近更新 ${fmtTime(annotation.updated_at)} · 总分 ${annotation.total} · ${annotation.annotator}`
      : "尚未评分";
    renderItems(items);
  }

  function renderItems(items) {
    $("items-empty").hidden = items.length > 0;
    $("item-list").innerHTML = items.map((item) => `
      <li>
        <span class="badge">${esc(item.label)}</span>
        <span class="muted">${esc(item.block_ref)}</span>
        <span class="grow">${esc(item.comment || "")}</span>
        <button class="ghost small danger" data-remove-item="${esc(item.id)}">删除</button>
      </li>`).join("");
    $("item-list").querySelectorAll("[data-remove-item]").forEach((button) => {
      button.addEventListener("click", () => removeItem(button.dataset.removeItem));
    });
  }

  async function loadAnnotationForCurrent() {
    if (!state.selected) return;
    try {
      const data = await api(
        `/api/jobs/${encodeURIComponent(state.selected)}/annotation?annotator=${encodeURIComponent(state.annotator)}`
      );
      const annotation = data.annotation || (data.annotations || [])[0] || null;
      renderAnnotation(annotation, data.items || []);
    } catch (error) {
      toast(`加载标注失败：${error.message}`, true);
    }
  }

  async function saveAnnotation() {
    if (!state.selected) return;
    const annotator = $("annotator").value.trim();
    if (!annotator) { toast("请填写标注人", true); return; }
    state.annotator = annotator;
    localStorage.setItem("hmu.annotator", annotator);
    const scores = {};
    SCORE_FIELDS.forEach((field) => { scores[field.key] = Number($("score-" + field.key).value); });
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(state.selected)}/annotation`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          annotator,
          scores,
          comment: $("annotation-comment").value,
          status: $("annotation-status").value,
        }),
      });
      renderAnnotation(data.annotation, state.result ? state.result.annotation_items : []);
      toast(`评分已保存（总分 ${data.annotation.total}）`);
      refreshJobs();
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function addItem() {
    if (!state.selected) return;
    const blockRef = $("item-block").value.trim();
    if (!blockRef) { toast("请填写内容块引用，或从内容块点击「标注」", true); return; }
    const page = Number($("item-block").dataset.page ?? (blockRef.match(/^p(\d+)/) || [])[1] ?? 0);
    try {
      await api(
        `/api/jobs/${encodeURIComponent(state.selected)}/annotation/items?annotator=${encodeURIComponent(state.annotator)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            page_idx: page,
            block_ref: blockRef,
            label: $("item-label").value,
            comment: $("item-comment").value,
          }),
        }
      );
      $("item-block").value = "";
      $("item-comment").value = "";
      await loadAnnotationForCurrent();
      toast("元素标注已添加");
    } catch (error) {
      toast(`添加失败：${error.message}`, true);
    }
  }

  async function removeItem(itemId) {
    try {
      await api(`/api/jobs/${encodeURIComponent(state.selected)}/annotation/items/${encodeURIComponent(itemId)}`, {
        method: "DELETE",
      });
      await loadAnnotationForCurrent();
      toast("已删除标注");
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  /* ------------------------------------------------------------------- log */
  async function loadLog(reset) {
    if (!state.selected) return;
    const jobId = state.selected;
    if (reset) {
      state.logOffset = 0;
      $("log-box").textContent = "";
    }
    try {
      const data = await api(
        `/api/jobs/${encodeURIComponent(jobId)}/log?offset=${state.logOffset}&limit=800`
      );
      if (state.selected !== jobId) return;
      if (data.lines && data.lines.length) {
        const box = $("log-box");
        const prefix = box.textContent ? box.textContent + "\n" : "";
        box.textContent = prefix + data.lines.join("\n");
        state.logOffset = data.next_offset;
        if ($("log-follow").checked) box.scrollTop = box.scrollHeight;
      }
      const job = state.job;
      $("log-status").textContent = job
        ? `状态 ${job.status} · 已读取 ${state.logOffset} 字符`
        : `已读取 ${state.logOffset} 字符`;
    } catch (error) {
      $("log-status").textContent = `日志读取失败：${error.message}`;
    }
  }

  /* --------------------------------------------------------------- actions */
  async function cancelJob(jobId) {
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
      toast("已请求取消任务");
      await refreshJobs();
      if (state.selected === jobId) await loadJobDetail(jobId);
    } catch (error) {
      toast(`取消失败：${error.message}`, true);
    }
  }

  async function deleteJob(jobId) {
    if (!confirm(`确定删除任务 ${jobId.slice(0, 8)} 及其产物？`)) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      toast("任务已删除");
      if (state.selected === jobId) {
        state.selected = null;
        state.job = null;
        state.result = null;
        $("result-panel").hidden = true;
      }
      await refreshJobs();
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  /* ------------------------------------------------------------ evaluation */
  async function loadEvaluation() {
    await Promise.all([loadAnnotations(), loadStats(), loadDocuments()]);
  }

  async function loadAnnotations() {
    const engine = $("eval-engine-filter").value;
    const annotator = $("eval-annotator-filter").value.trim();
    const params = new URLSearchParams();
    if (engine) params.set("engine", engine);
    if (annotator) params.set("annotator", annotator);
    try {
      const data = await api(`/api/annotations?${params.toString()}`);
      const rows = data.annotations || [];
      renderAnnotationTable(rows);
      renderEvalCards(rows);
    } catch (error) {
      toast(`加载评分失败：${error.message}`, true);
    }
  }

  function renderEvalCards(rows) {
    const count = rows.length;
    const avg = count ? rows.reduce((sum, row) => sum + Number(row.total || 0), 0) / count : 0;
    const submitted = rows.filter((row) => row.status === "submitted").length;
    const engines = {};
    rows.forEach((row) => {
      const key = row.engine || "unknown";
      engines[key] = engines[key] || { total: 0, count: 0 };
      engines[key].total += Number(row.total || 0);
      engines[key].count += 1;
    });
    let best = "—";
    let bestAvg = -1;
    Object.entries(engines).forEach(([key, value]) => {
      const value2 = value.total / value.count;
      if (value2 > bestAvg) { bestAvg = value2; best = `${key} (${value2.toFixed(2)})`; }
    });
    $("eval-cards").innerHTML = `
      <div class="stat-card"><div class="label">评分记录</div><div class="value">${count}</div>
        <div class="sub">已提交 ${submitted} 条</div></div>
      <div class="stat-card"><div class="label">平均总分</div><div class="value">${avg.toFixed(2)}</div>
        <div class="sub">五维均值（1–5 分）</div></div>
      <div class="stat-card"><div class="label">当前最佳引擎</div>
        <div class="value" style="font-size:18px;margin-top:10px">${esc(best)}</div>
        <div class="sub">按平均总分</div></div>`;
  }

  function renderAnnotationTable(rows) {
    $("annotations-empty").hidden = rows.length > 0;
    $("annotations-table").querySelector("tbody").innerHTML = rows.map((row) => {
      const scores = row.scores || {};
      return `<tr class="static">
        <td>${esc(row.filename || row.job_id)}</td>
        <td><span class="badge engine">${esc(row.engine || "—")}</span></td>
        <td>${esc(row.annotator)}</td>
        <td>${esc(scores.text_accuracy ?? "—")}</td>
        <td>${esc(scores.reading_order ?? "—")}</td>
        <td>${esc(scores.table ?? "—")}</td>
        <td>${esc(scores.formula ?? "—")}</td>
        <td>${esc(scores.image ?? "—")}</td>
        <td><span class="badge score">${esc(row.total)}</span></td>
        <td>${esc(row.status)}</td>
        <td class="muted tiny">${fmtTime(row.updated_at)}</td>
      </tr>`;
    }).join("");
  }

  async function loadStats() {
    const groupBy = $("stats-groupby").value;
    try {
      const data = await api(`/api/stats/scores?group_by=${encodeURIComponent(groupBy)}`);
      const rows = data.rows || [];
      $("stats-empty").hidden = rows.length > 0;
      const columns = data.group_by || groupBy.split(",");
      const head = $("stats-table").querySelector("thead");
      head.innerHTML = `<tr><th>分组</th><th>样本</th><th>总分</th><th>文本</th><th>顺序</th><th>表格</th><th>公式</th><th>图片</th></tr>`;
      $("stats-table").querySelector("tbody").innerHTML = rows.map((row) => {
        const group = columns
          .filter((column) => !(row.engine && row.engine !== "mineru" && ["backend", "method"].includes(column)))
          .map((column) => row[column])
          .filter((v) => v !== null && v !== undefined && v !== "")
          .join(" / ") || "全部";
        return `<tr class="static">
          <td>${esc(group)}</td>
          <td>${esc(row.samples)}</td>
          <td><span class="badge score">${esc(row.avg_total)}</span></td>
          <td>${esc(row.avg_text_accuracy ?? "—")}</td>
          <td>${esc(row.avg_reading_order ?? "—")}</td>
          <td>${esc(row.avg_table ?? "—")}</td>
          <td>${esc(row.avg_formula ?? "—")}</td>
          <td>${esc(row.avg_image ?? "—")}</td>
        </tr>`;
      }).join("");
    } catch (error) {
      toast(`加载统计失败：${error.message}`, true);
    }
  }

  async function loadDocuments() {
    try {
      const data = await api("/api/documents?limit=200");
      state.documents = data.documents || [];
      const select = $("compare-document");
      const current = select.value;
      select.innerHTML = state.documents.map((doc) =>
        `<option value="${esc(doc.sha256)}">${esc(doc.filename)} · ${doc.run_count} 次 · ${esc(doc.sha256.slice(0, 8))}</option>`
      ).join("");
      if (current) select.value = current;
      if (!state.documents.length) {
        $("compare-picker").innerHTML = `<p class="empty">暂无文档。先提交解析任务。</p>`;
      } else if (!select.value) {
        select.value = state.documents[0].sha256;
      }
      await loadDocumentRuns();
    } catch (error) {
      toast(`加载文档失败：${error.message}`, true);
    }
  }

  async function loadDocumentRuns() {
    const sha = $("compare-document").value;
    if (!sha) return;
    try {
      const data = await api(`/api/documents/${encodeURIComponent(sha)}/runs`);
      const runs = (data.runs || []).filter((run) => run.status === "succeeded");
      state.compareRuns = runs;
      state.compare = { left: null, right: null };
      $("compare-left").innerHTML = "";
      $("compare-right").innerHTML = "";
      $("compare-left-title").textContent = "选择左侧运行";
      $("compare-right-title").textContent = "选择右侧运行";
      if (!runs.length) {
        $("compare-picker").innerHTML = `<p class="empty">该文档暂无成功的解析运行。</p>`;
        return;
      }
      $("compare-picker").innerHTML = runs.map((run, index) => `
        <div class="compare-run">
          <span class="muted tiny">${index + 1}</span>
          <span>${esc(run.id)} · <span class="badge engine">${esc(run.engine)}</span>
            <span class="muted tiny">${esc(run.runner)}</span></span>
          <span class="muted tiny">${fmtDuration(run.duration_ms)}</span>
          <span>${run.score_total !== null && run.score_total !== undefined
            ? `<span class="badge score">${esc(run.score_total)}</span>` : ""}</span>
          <span>
            <label class="checkbox"><input type="radio" name="compare-left" value="${esc(run.id)}" /> 左</label>
            <label class="checkbox"><input type="radio" name="compare-right" value="${esc(run.id)}" /> 右</label>
          </span>
        </div>`).join("");
      $("compare-picker").querySelectorAll('input[name="compare-left"]').forEach((input) => {
        input.addEventListener("change", () => renderCompareSide("left", input.value));
      });
      $("compare-picker").querySelectorAll('input[name="compare-right"]').forEach((input) => {
        input.addEventListener("change", () => renderCompareSide("right", input.value));
      });
    } catch (error) {
      toast(`加载运行失败：${error.message}`, true);
    }
  }

  async function renderCompareSide(side, jobId) {
    state.compare[side] = jobId;
    const titleId = side === "left" ? "compare-left-title" : "compare-right-title";
    const bodyId = side === "left" ? "compare-left" : "compare-right";
    $(titleId).textContent = `运行 ${jobId} 加载中…`;
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/result`);
      const job = result.job || {};
      $(titleId).innerHTML = `<span class="badge engine">${esc(job.engine)}</span>
        <span class="muted">${esc(job.filename)} · ${fmtDuration(job.duration_ms)}</span>`;
      renderMarkdown($(bodyId), result.markdown || "");
    } catch (error) {
      $(titleId).textContent = `加载失败：${error.message}`;
    }
  }

  /* ---------------------------------------------------------------- workers */
  async function loadHealth() {
    try {
      const health = await api("/api/health");
      const total = Object.values(health.queue || {}).reduce((sum, value) => sum + Number(value || 0), 0);
      const pill = $("health-pill");
      pill.textContent = `MinerU ${health.mineru_version} · ${health.device}${health.cuda ? " (CUDA)" : ""} · 队列 ${total} · Worker ${health.workers_online}/${health.workers_total}`;
      pill.className = "health ok";
      state.health = health;
    } catch {
      $("health-pill").textContent = "服务不可用";
      $("health-pill").className = "health warn";
    }
  }

  async function loadWorkers() {
    try {
      const [health, data] = await Promise.all([api("/api/health"), api("/api/workers")]);
      state.health = health;
      state.workers = data.workers || [];
      renderWorkers();
    } catch (error) {
      toast(`加载节点失败：${error.message}`, true);
    }
  }

  function renderWorkers() {
    const health = state.health;
    if (health) {
      const queue = health.queue || {};
      $("worker-cards").innerHTML = `
        <div class="stat-card"><div class="label">当前角色</div><div class="value" style="font-size:20px">${esc(health.role)}</div>
          <div class="sub">both / coordinator / worker</div></div>
        <div class="stat-card"><div class="label">在线 Worker</div><div class="value">${health.workers_online}/${health.workers_total}</div>
          <div class="sub">心跳阈值 30s</div></div>
        <div class="stat-card"><div class="label">队列</div><div class="value">${queue.queued || 0} / ${queue.running || 0}</div>
          <div class="sub">排队中 / 运行中</div></div>
        <div class="stat-card"><div class="label">已完成</div><div class="value">${queue.succeeded || 0}</div>
          <div class="sub">失败 ${queue.failed || 0} · 取消 ${queue.canceled || 0}</div></div>
        <div class="stat-card"><div class="label">MinerU</div><div class="value" style="font-size:18px;margin-top:8px">${esc(health.mineru_version)}</div>
          <div class="sub">${esc(health.device)}${health.cuda ? " · CUDA 可用" : " · 无 CUDA"}</div></div>`;
      $("deploy-hint").textContent = [
        "# 单机（默认，内置 Worker）：",
        "python -m app",
        "",
        "# 协调节点（对外提供 API，不执行本地解析）：",
        '$env:HMU_ROLE="coordinator"; $env:HMU_HOST="0.0.0.0"; python -m app',
        "",
        "# 解析节点（从协调节点拉取任务）：",
        '$env:HMU_ROLE="worker"',
        '$env:HMU_COORDINATOR_URL="http://<coordinator>:8000"',
        `$env:HMU_WORKER_TOKEN="<见 ${health.worker_token_file}>"`,
        "python -m app",
        "",
        `# 数据目录：${health.data_dir}`,
      ].join("\n");
    }
    const workers = state.workers || [];
    $("workers-empty").hidden = workers.length > 0;
    $("workers-table").querySelector("tbody").innerHTML = workers.map((worker) => {
      const caps = worker.capabilities || {};
      return `<tr class="static">
        <td><strong>${esc(worker.name)}</strong><div class="muted tiny">${esc(worker.id)}</div></td>
        <td>${esc(worker.hostname || "—")}</td>
        <td><span class="badge ${worker.status === "online" ? "succeeded" : "canceled"}">${esc(worker.status)}</span></td>
        <td class="tiny">${esc((caps.engines || []).join(", "))}</td>
        <td class="tiny">${esc((caps.backends || []).join(", "))}</td>
        <td>${worker.current_job_id ? `<code>${esc(worker.current_job_id.slice(0, 8))}</code>` : "—"}</td>
        <td>${esc(worker.completed)} / ${esc(worker.failed)}</td>
        <td class="muted tiny">${fmtTime(worker.last_heartbeat)}</td>
      </tr>`;
    }).join("");
  }

  /* ------------------------------------------------------------------ loop */
  async function tick() {
    state.tick += 1;
    if (state.tick % 5 === 1) loadHealth();
    if ($("view-workbench").classList.contains("active")) {
      await refreshJobs();
      if (state.selected) {
        const summary = state.jobs.find((job) => job.id === state.selected);
        const status = summary ? summary.status : null;
        if (status && ["queued", "running"].includes(status)) {
          await loadJobDetail(state.selected);
          if ($("tab-log").classList.contains("active")) await loadLog(false);
        } else if (status && state.lastStatus && state.lastStatus !== status && status !== "running") {
          await loadJobDetail(state.selected);
          await loadResult(state.selected);
          await loadLog(false);
        }
        if (status) state.lastStatus = status;
      }
    }
    if ($("view-workers").classList.contains("active") && state.tick % 3 === 0) loadWorkers();
  }

  /* ------------------------------------------------------------------ init */
  function bindEvents() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => showView(tab.dataset.view));
    });
    document.querySelectorAll(".subtab").forEach((tab) => {
      tab.addEventListener("click", () => showResultTab(tab.dataset.tab));
    });

    const dropzone = $("dropzone");
    const fileInput = $("file-input");
    dropzone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const file = fileInput.files[0];
      state.file = file;
      handleFile(file);
    });
    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer.files[0];
      if (file) { state.file = file; handleFile(file); }
    });

    $("engine-select").addEventListener("change", updateEngineHint);
    $("submit-btn").addEventListener("click", submitJob);
    $("refresh-jobs").addEventListener("click", refreshJobs);
    $("toggle-raw").addEventListener("click", () => {
      const raw = $("markdown-raw");
      const rendered = $("markdown-rendered");
      const showRaw = raw.hidden;
      raw.hidden = !showRaw;
      rendered.hidden = showRaw;
      $("toggle-raw").textContent = showRaw ? "查看渲染结果" : "查看原始 Markdown";
    });
    $("cancel-job").addEventListener("click", () => state.selected && cancelJob(state.selected));
    $("delete-job").addEventListener("click", () => state.selected && deleteJob(state.selected));
    $("save-annotation").addEventListener("click", saveAnnotation);
    $("add-item").addEventListener("click", addItem);

    $("refresh-annotations").addEventListener("click", loadAnnotations);
    $("eval-engine-filter").addEventListener("change", loadAnnotations);
    $("eval-annotator-filter").addEventListener("change", loadAnnotations);
    $("refresh-stats").addEventListener("click", loadStats);
    $("stats-groupby").addEventListener("change", loadStats);
    $("refresh-documents").addEventListener("click", loadDocuments);
    $("compare-document").addEventListener("change", loadDocumentRuns);
    $("refresh-workers").addEventListener("click", loadWorkers);
  }

  async function init() {
    bindEvents();
    initAnnotationForm();
    updateEngineHint();
    try {
      await loadParsers();
      const filters = $("eval-engine-filter");
      filters.innerHTML = `<option value="">全部引擎</option>` +
        state.parsers
          .filter((parser) => parser.id !== "auto")
          .map((parser) => `<option value="${esc(parser.id)}">${esc(parser.label)}</option>`)
          .join("");
    } catch (error) {
      toast(`初始化失败：${error.message}`, true);
    }
    await loadHealth();
    await refreshJobs();
    setInterval(tick, 3000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
