const bridge = window.AstrBotPluginPage;

const state = {
  images: [],
  categories: [],
  editing: null,
};

const els = {
  status: document.getElementById("status"),
  refresh: document.getElementById("refresh"),
  statImages: document.getElementById("stat-images"),
  statCategories: document.getElementById("stat-categories"),
  statSends: document.getElementById("stat-sends"),
  statLatest: document.getElementById("stat-latest"),
  search: document.getElementById("search"),
  category: document.getElementById("category"),
  safetyStatus: document.getElementById("safety-status"),
  grid: document.getElementById("grid"),
  uploadForm: document.getElementById("upload-form"),
  uploadCategory: document.getElementById("upload-category"),
  uploadFile: document.getElementById("upload-file"),
  editor: document.getElementById("editor"),
  closeEditor: document.getElementById("close-editor"),
  saveEditor: document.getElementById("save-editor"),
  editId: document.getElementById("edit-id"),
  editTitle: document.getElementById("edit-title"),
  editDescription: document.getElementById("edit-description"),
  editTags: document.getElementById("edit-tags"),
  editRating: document.getElementById("edit-rating"),
  editSafetyStatus: document.getElementById("edit-safety-status"),
  editSendTransform: document.getElementById("edit-send-transform"),
};

await bridge.ready();
await loadAll();

els.refresh.addEventListener("click", loadAll);
els.search.addEventListener("input", debounce(loadImages, 250));
els.category.addEventListener("change", loadImages);
els.safetyStatus.addEventListener("change", loadImages);
els.closeEditor.addEventListener("click", () => els.editor.close());
els.saveEditor.addEventListener("click", saveEditor);
els.uploadForm.addEventListener("submit", uploadImage);
els.editSafetyStatus.addEventListener("change", () => {
  if (els.editSafetyStatus.value === "sensitive" && els.editSendTransform.value === "none") {
    els.editSendTransform.value = "rotate_180";
  }
});

async function loadAll() {
  setStatus("正在加载...");
  try {
    await Promise.all([loadStats(), loadCategories()]);
    await loadImages();
    setStatus("已同步");
  } catch (error) {
    setStatus(error.message || String(error));
  }
}

async function loadStats() {
  const result = await bridge.apiGet("stats");
  assertOk(result);
  const stats = result.data;
  els.statImages.textContent = stats.image_count ?? 0;
  els.statCategories.textContent = stats.category_count ?? 0;
  els.statSends.textContent = stats.send_count ?? 0;
  els.statLatest.textContent = formatDate(stats.latest_upload);
}

async function loadCategories() {
  const result = await bridge.apiGet("categories");
  assertOk(result);
  state.categories = result.data || [];
  const current = els.category.value;
  els.category.innerHTML = `<option value="">全部分类</option>`;
  for (const item of state.categories) {
    const option = document.createElement("option");
    option.value = item.category;
    option.textContent = `${item.category} (${item.image_count})`;
    els.category.appendChild(option);
  }
  els.category.value = current;
}

async function loadImages() {
  const params = {
    query: els.search.value.trim(),
    category: els.category.value,
    safety_status: els.safetyStatus.value,
    limit: 120,
  };
  const result = await bridge.apiGet("images", params);
  assertOk(result);
  state.images = result.data || [];
  renderImages();
}

function renderImages() {
  els.grid.innerHTML = "";
  if (state.images.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无图片。可以使用 /friupload 分类名，或在这里选择分类和文件上传。";
    els.grid.appendChild(empty);
    return;
  }
  for (const image of state.images) {
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <div class="thumb"><img alt="" loading="lazy" src="${escapeAttr(image.preview_url)}"></div>
      <div class="body">
        <h3>${escapeHtml(image.title || image.short_id)}</h3>
        <div class="meta">分类：${escapeHtml(image.category)} · ${safetyLabel(image.safety_status)} · ${transformLabel(image.send_transform)} · 发送 ${image.send_count || 0} 次</div>
        <div class="meta">ID：${escapeHtml(image.short_id)}</div>
        <div class="tags">${renderTags(image.tags)}</div>
        <div class="meta">${escapeHtml(image.description || "未填写描述")}</div>
        <footer><button type="button" data-edit="${escapeAttr(image.id)}">编辑</button></footer>
      </div>
    `;
    card.querySelector("[data-edit]").addEventListener("click", () => openEditor(image));
    els.grid.appendChild(card);
  }
}

function openEditor(image) {
  state.editing = image;
  els.editId.value = image.id;
  els.editTitle.value = image.title || "";
  els.editDescription.value = image.description || "";
  els.editTags.value = (image.tags || []).join(" ");
  els.editRating.value = image.rating ?? "";
  els.editSafetyStatus.value = image.safety_status || "normal";
  els.editSendTransform.value = image.send_transform || "none";
  els.editor.showModal();
}

async function saveEditor() {
  const payload = {
    id: els.editId.value,
    title: els.editTitle.value,
    description: els.editDescription.value,
    tags: splitTags(els.editTags.value),
    safety_status: els.editSafetyStatus.value,
    send_transform: els.editSendTransform.value,
  };
  if (els.editRating.value !== "") {
    payload.rating = Number(els.editRating.value);
  }
  const result = await bridge.apiPost("image/update", payload);
  assertOk(result);
  els.editor.close();
  await Promise.all([loadStats(), loadCategories(), loadImages()]);
  setStatus("已保存");
}

async function uploadImage(event) {
  event.preventDefault();
  const file = els.uploadFile.files[0];
  if (!file) {
    setStatus("请选择要上传的图片。");
    return;
  }
  const category = encodeURIComponent((els.uploadCategory.value || "默认").trim());
  setStatus("正在上传...");
  const result = await bridge.upload(`upload/${category}`, file);
  assertOk(result);
  els.uploadFile.value = "";
  await loadAll();
  setStatus(result.status === "duplicate" ? "图片已存在" : "上传完成");
}

function renderTags(tags) {
  if (!tags || tags.length === 0) {
    return `<span class="tag">未标记</span>`;
  }
  return tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
}

function splitTags(value) {
  return value
    .split(/[\s,，;；#]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function safetyLabel(value) {
  return {
    normal: "正常",
    sensitive: "敏感",
    hidden: "隐藏",
  }[value] || value || "正常";
}

function transformLabel(value) {
  return {
    none: "不变换",
    rotate_180: "旋转180度",
  }[value] || value || "不变换";
}

function assertOk(result) {
  if (!result || result.ok !== true) {
    throw new Error(result?.error || "请求失败");
  }
}

function setStatus(text) {
  els.status.textContent = text;
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function debounce(fn, delay) {
  let timer = null;
  return () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(fn, delay);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}
