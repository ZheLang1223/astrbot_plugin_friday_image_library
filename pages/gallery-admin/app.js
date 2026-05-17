const bridge = window.AstrBotPluginPage;

const PAGE_SIZE = 60;

const state = {
  images: [],
  categories: [],
  selected: new Set(),
  editing: null,
  offset: 0,
  hasMore: true,
  loading: false,
};

const els = {
  status: document.getElementById("status"),
  refresh: document.getElementById("refresh"),
  statImages: document.getElementById("stat-images"),
  statCategories: document.getElementById("stat-categories"),
  statSends: document.getElementById("stat-sends"),
  statInbox: document.getElementById("stat-inbox"),
  search: document.getElementById("search"),
  category: document.getElementById("category"),
  safetyStatus: document.getElementById("safety-status"),
  filterInbox: document.getElementById("filter-inbox"),
  grid: document.getElementById("grid"),
  sentinel: document.getElementById("sentinel"),
  uploadForm: document.getElementById("upload-form"),
  uploadCategory: document.getElementById("upload-category"),
  uploadFile: document.getElementById("upload-file"),
  dropZone: document.getElementById("drop-zone"),
  bulkBar: document.getElementById("bulk-bar"),
  selectedCount: document.getElementById("selected-count"),
  bulkSafetyStatus: document.getElementById("bulk-safety-status"),
  bulkSendTransform: document.getElementById("bulk-send-transform"),
  bulkCategory: document.getElementById("bulk-category"),
  bulkMove: document.getElementById("bulk-move"),
  bulkTags: document.getElementById("bulk-tags"),
  bulkTagOperation: document.getElementById("bulk-tag-operation"),
  bulkTagsApply: document.getElementById("bulk-tags-apply"),
  bulkApply: document.getElementById("bulk-apply"),
  bulkDelete: document.getElementById("bulk-delete"),
  bulkClear: document.getElementById("bulk-clear"),
  categoryCreateForm: document.getElementById("category-create-form"),
  categoryCreateName: document.getElementById("category-create-name"),
  categoryRenameForm: document.getElementById("category-rename-form"),
  categoryRenameSource: document.getElementById("category-rename-source"),
  categoryRenameName: document.getElementById("category-rename-name"),
  categoryMergeForm: document.getElementById("category-merge-form"),
  categoryMergeSource: document.getElementById("category-merge-source"),
  categoryMergeTarget: document.getElementById("category-merge-target"),
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
bindEvents();
await loadAll();

function bindEvents() {
  els.refresh.addEventListener("click", loadAll);
  els.search.addEventListener("input", debounce(() => loadImages({ reset: true }), 250));
  els.category.addEventListener("change", () => loadImages({ reset: true }));
  els.safetyStatus.addEventListener("change", () => loadImages({ reset: true }));
  els.filterInbox.addEventListener("click", filterInbox);
  els.closeEditor.addEventListener("click", () => els.editor.close());
  els.saveEditor.addEventListener("click", saveEditor);
  els.uploadForm.addEventListener("submit", uploadSelectedFiles);
  els.uploadFile.addEventListener("change", () => {
    const count = els.uploadFile.files.length;
    setStatus(count ? `已选择 ${count} 个文件` : "未选择文件");
  });
  els.dropZone.addEventListener("dragover", onDragOver);
  els.dropZone.addEventListener("dragleave", onDragLeave);
  els.dropZone.addEventListener("drop", uploadDroppedFiles);
  els.bulkApply.addEventListener("click", applyBulkUpdate);
  els.bulkMove.addEventListener("click", moveSelectedCategory);
  els.bulkTagsApply.addEventListener("click", applyBulkTags);
  els.bulkDelete.addEventListener("click", deleteSelected);
  els.bulkClear.addEventListener("click", clearSelection);
  els.categoryCreateForm.addEventListener("submit", createCategory);
  els.categoryRenameForm.addEventListener("submit", renameCategory);
  els.categoryMergeForm.addEventListener("submit", mergeCategory);
  els.editSafetyStatus.addEventListener("change", () => {
    if (els.editSafetyStatus.value === "sensitive" && els.editSendTransform.value === "none") {
      els.editSendTransform.value = "rotate_180";
    }
  });

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      loadImages();
    }
  });
  observer.observe(els.sentinel);
}

async function loadAll() {
  setStatus("正在加载...");
  try {
    await Promise.all([loadStats(), loadCategories()]);
    await loadImages({ reset: true });
    setStatus("已同步");
  } catch (error) {
    setStatus(error.message || String(error));
  }
}

async function loadStats() {
  const [statsResult, inboxResult] = await Promise.all([
    bridge.apiGet("stats"),
    bridge.apiGet("inbox/stats"),
  ]);
  assertOk(statsResult);
  assertOk(inboxResult);
  const stats = statsResult.data;
  els.statImages.textContent = stats.image_count ?? 0;
  els.statCategories.textContent = stats.category_count ?? 0;
  els.statSends.textContent = stats.send_count ?? 0;
  els.statInbox.textContent = inboxResult.data?.count ?? 0;
}

async function loadCategories() {
  const result = await bridge.apiGet("categories");
  assertOk(result);
  state.categories = result.data || [];
  const current = els.category.value;
  els.category.innerHTML = `<option value="">全部分类</option>`;
  for (const item of state.categories) {
    const option = document.createElement("option");
    option.value = item.slug || item.category;
    option.textContent = `${item.category} (${item.image_count})`;
    els.category.appendChild(option);
  }
  els.category.value = current;
  renderCategorySelects();
}

function renderCategorySelects() {
  const selects = [
    els.categoryRenameSource,
    els.categoryMergeSource,
    els.categoryMergeTarget,
  ];
  for (const select of selects) {
    const current = select.value;
    select.innerHTML = "";
    for (const item of state.categories) {
      const option = document.createElement("option");
      option.value = item.slug || item.category;
      option.textContent = `${item.category} (${item.image_count})`;
      select.appendChild(option);
    }
    select.value = current;
  }
}

function filterInbox() {
  const inbox = state.categories.find((item) => item.slug === "inbox");
  els.category.value = inbox ? "inbox" : "inbox";
  loadImages({ reset: true });
}

async function loadImages({ reset = false } = {}) {
  if (state.loading || (!state.hasMore && !reset)) {
    return;
  }
  if (reset) {
    state.images = [];
    state.offset = 0;
    state.hasMore = true;
    state.selected.clear();
    renderImages();
    renderBulkBar();
  }
  state.loading = true;
  setStatus("正在加载图片...");
  try {
    const params = {
      query: els.search.value.trim(),
      category: els.category.value,
      safety_status: els.safetyStatus.value,
      limit: PAGE_SIZE,
      offset: state.offset,
    };
    const result = await bridge.apiGet("images", params);
    assertOk(result);
    const items = result.data || [];
    state.images.push(...items);
    state.offset += items.length;
    state.hasMore = items.length === PAGE_SIZE;
    renderImages();
    setStatus(state.hasMore ? "继续向下滚动加载" : "已加载全部");
  } catch (error) {
    setStatus(error.message || String(error));
  } finally {
    state.loading = false;
  }
}

function renderImages() {
  els.grid.innerHTML = "";
  if (state.images.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无图片。可以使用 /friup 分类名，或在这里选择分类和文件上传。";
    els.grid.appendChild(empty);
    return;
  }
  for (const image of state.images) {
    const card = document.createElement("article");
    card.className = `card${state.selected.has(image.id) ? " selected" : ""}`;
    card.innerHTML = `
      <label class="select-box">
        <input type="checkbox" data-select="${escapeAttr(image.id)}" ${state.selected.has(image.id) ? "checked" : ""} />
      </label>
      <div class="thumb"><img alt="" loading="lazy" src="${escapeAttr(image.preview_url)}"></div>
      <div class="body">
        <h3>${escapeHtml(image.title || image.short_id)}</h3>
        <div class="meta">分类：${escapeHtml(image.category_display_name || image.category)} · ${safetyLabel(image.safety_status)} · ${transformLabel(image.send_transform)} · 发送 ${image.send_count || 0} 次</div>
        <div class="meta">ID：${escapeHtml(image.short_id)} · ${escapeHtml(image.category_slug || "")}</div>
        <div class="tags">${renderTags(image.tags)}</div>
        <div class="meta">${escapeHtml(image.description || "未填写描述")}</div>
        <footer><button type="button" data-edit="${escapeAttr(image.id)}">编辑</button></footer>
      </div>
    `;
    card.querySelector("[data-edit]").addEventListener("click", () => openEditor(image));
    card.querySelector("[data-select]").addEventListener("change", (event) => {
      if (event.target.checked) {
        state.selected.add(image.id);
      } else {
        state.selected.delete(image.id);
      }
      renderBulkBar();
      card.classList.toggle("selected", event.target.checked);
    });
    els.grid.appendChild(card);
  }
}

function renderBulkBar() {
  const count = state.selected.size;
  els.bulkBar.hidden = count === 0;
  els.selectedCount.textContent = String(count);
}

function clearSelection() {
  state.selected.clear();
  renderImages();
  renderBulkBar();
}

async function applyBulkUpdate() {
  const updates = {};
  if (els.bulkSafetyStatus.value) {
    updates.safety_status = els.bulkSafetyStatus.value;
  }
  if (els.bulkSendTransform.value) {
    updates.send_transform = els.bulkSendTransform.value;
  }
  if (updates.safety_status === "sensitive" && !updates.send_transform) {
    updates.send_transform = "rotate_180";
  }
  if (Object.keys(updates).length === 0) {
    setStatus("请选择要批量修改的字段。");
    return;
  }
  setStatus("正在批量更新...");
  const result = await bridge.apiPost("image/batch-update", {
    ids: Array.from(state.selected),
    updates,
  });
  assertOk(result);
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  const failed = result.data.failed?.length || 0;
  setStatus(`已更新 ${result.data.updated || 0} 张${failed ? `，失败 ${failed} 张` : ""}`);
}

async function moveSelectedCategory() {
  const category = els.bulkCategory.value.trim();
  if (!category) {
    setStatus("请输入目标分类。");
    return;
  }
  setStatus("正在移动分类...");
  const result = await bridge.apiPost("image/batch-move-category", {
    ids: Array.from(state.selected),
    category,
  });
  assertOk(result);
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  const failed = result.data.failed?.length || 0;
  setStatus(`已移动 ${result.data.updated || 0} 张${failed ? `，失败 ${failed} 张` : ""}`);
}

async function applyBulkTags() {
  const tags = splitTags(els.bulkTags.value);
  if (!tags.length) {
    setStatus("请输入标签。");
    return;
  }
  setStatus("正在批量处理标签...");
  const result = await bridge.apiPost("image/batch-tags", {
    ids: Array.from(state.selected),
    tags,
    operation: els.bulkTagOperation.value,
  });
  assertOk(result);
  await loadImages({ reset: true });
  const failed = result.data.failed?.length || 0;
  setStatus(`已处理 ${result.data.updated || 0} 张${failed ? `，失败 ${failed} 张` : ""}`);
}

async function deleteSelected() {
  if (!window.confirm(`确认删除 ${state.selected.size} 张图片？`)) {
    return;
  }
  setStatus("正在删除...");
  const result = await bridge.apiPost("image/batch-delete", {
    ids: Array.from(state.selected),
  });
  assertOk(result);
  clearSelection();
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  const failed = result.data.failed?.length || 0;
  setStatus(`已删除 ${result.data.deleted || 0} 张${failed ? `，失败 ${failed} 张` : ""}`);
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
    rating: els.editRating.value,
    safety_status: els.editSafetyStatus.value,
    send_transform: els.editSendTransform.value,
  };
  const result = await bridge.apiPost("image/update", payload);
  assertOk(result);
  els.editor.close();
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  setStatus("已保存");
}

async function uploadSelectedFiles(event) {
  event.preventDefault();
  await uploadFiles(Array.from(els.uploadFile.files));
  els.uploadFile.value = "";
}

async function uploadDroppedFiles(event) {
  event.preventDefault();
  els.dropZone.classList.remove("dragging");
  const files = Array.from(event.dataTransfer.files || []).filter((file) =>
    file.type.startsWith("image/")
  );
  await uploadFiles(files);
}

async function uploadFiles(files) {
  if (!files.length) {
    setStatus("请选择要上传的图片。");
    return;
  }
  const category = encodeURIComponent(els.uploadCategory.value.trim());
  let saved = 0;
  let duplicates = 0;
  let failed = 0;
  for (const [index, file] of files.entries()) {
    setStatus(`正在上传 ${index + 1}/${files.length}...`);
    const endpoint = category ? `upload/${category}` : "upload";
    const result = await bridge.upload(endpoint, file);
    assertOk(result);
    const data = result.data || result;
    saved += data.saved_count || (data.status === "saved" ? 1 : 0);
    duplicates += data.duplicate_count || (data.status === "duplicate" ? 1 : 0);
    failed += data.failed?.length || 0;
  }
  await loadAll();
  setStatus(`上传完成：新增 ${saved} 张，已存在 ${duplicates} 张${failed ? `，失败 ${failed} 张` : ""}`);
}

async function createCategory(event) {
  event.preventDefault();
  const category = els.categoryCreateName.value.trim();
  if (!category) {
    setStatus("请输入分类名称。");
    return;
  }
  const result = await bridge.apiPost("category/create", { category });
  assertOk(result);
  els.categoryCreateName.value = "";
  await Promise.all([loadStats(), loadCategories()]);
  setStatus("分类已创建");
}

async function renameCategory(event) {
  event.preventDefault();
  const category = els.categoryRenameSource.value;
  const displayName = els.categoryRenameName.value.trim();
  if (!category || !displayName) {
    setStatus("请选择分类并输入新显示名。");
    return;
  }
  const result = await bridge.apiPost("category/rename", {
    category,
    display_name: displayName,
  });
  assertOk(result);
  els.categoryRenameName.value = "";
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  setStatus("分类已重命名");
}

async function mergeCategory(event) {
  event.preventDefault();
  const source = els.categoryMergeSource.value;
  const target = els.categoryMergeTarget.value;
  if (!source || !target || source === target) {
    setStatus("请选择两个不同分类。");
    return;
  }
  if (!window.confirm("确认合并分类？源分类会被移除。")) {
    return;
  }
  const result = await bridge.apiPost("category/merge", {
    source_category: source,
    target_category: target,
  });
  assertOk(result);
  await Promise.all([loadStats(), loadCategories()]);
  await loadImages({ reset: true });
  setStatus(`已合并 ${result.data.moved || 0} 张图片`);
}

function onDragOver(event) {
  event.preventDefault();
  els.dropZone.classList.add("dragging");
}

function onDragLeave() {
  els.dropZone.classList.remove("dragging");
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
