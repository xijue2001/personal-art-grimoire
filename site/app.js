"use strict";

const $ = (id) => document.getElementById(id);
const state = {view: "gallery", source: {}, counts: {}, progress: {phase: "idle"}};
let railCollapsed = false;
let mainFeed;
let galleryPage = 1;
let galleryRequest;
let coverFeed;
let trashFeed;
let measuredWidth = 0;
let detailRequest = 0;
let searchTimer;
let scanTimer;
let toastTimer;
let pendingRemoval;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, action, className = "soft") {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", () => {
    Promise.resolve().then(action).catch((error) => toast(error.message));
  });
  return node;
}

function link(text, href) {
  const node = element("a", "soft", text);
  node.href = href;
  node.target = "_blank";
  node.rel = "noopener";
  return node;
}

function mediaUrl(digest, kind) {
  return "/media/" + digest + "/" + kind;
}

function toast(message) {
  const node = $("toast");
  const overlay = [...document.querySelectorAll("dialog[open]")].at(-1) || document.body;
  overlay.append(node);
  clearTimeout(toastTimer);
  node.textContent = message;
  node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, 3300);
}

async function api(path, data, signal) {
  const options = {signal};
  if (data) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "无法读取法典");
  return result;
}

async function copyText(text) {
  if (!text) {
    toast("这张图还没有可复制的提示词");
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const field = element("textarea", "clipboard-helper");
      field.value = text;
      field.readOnly = true;
      const previous = document.activeElement;
      const target = [...document.querySelectorAll("dialog[open]")].at(-1) || document.body;
      target.append(field);
      try {
        field.select();
        if (!document.execCommand("copy")) throw new Error("Clipboard unavailable");
      } finally {
        field.remove();
        if (previous instanceof HTMLElement) previous.focus({preventScroll: true});
      }
    }
    toast("已复制");
    return;
  } catch {
    const dialog = element("dialog");
    const header = element("div", "dialog-top");
    header.append(element("h2", "", "复制提示词"), button("关闭", () => dialog.close()));
    const body = element("div", "source-form");
    const input = element("textarea");
    input.value = text;
    input.readOnly = true;
    input.rows = 6;
    input.setAttribute("aria-label", "提示词");
    body.append(element("p", "muted", "请选择并复制下面的文本。"), input);
    dialog.append(header, body);
    document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove(), {once: true});
    dialog.showModal();
    input.select();
  }
}

function imageTitle(item) {
  return item.title || item.excerpt?.split(/[,，\n]/).filter((tag) => !tag.trim().startsWith("artist:")).slice(0, 2).join(" · ") ||
    item.positive?.split(/[,，\n]/).slice(0, 2).join(" · ") || "本机作品 " + item.digest.slice(0, 8);
}

function styleTitle(item) {
  return item.title || item.default_title || item.artist_text;
}

function picture(item, title, detail = false) {
  const frame = element("div", detail ? "picture detail-picture" : "picture");
  const placeholder = element("span", "", item.present ? "图片暂不可用" : "原图缺失");
  frame.append(placeholder);
  if (item.present) {
    placeholder.hidden = true;
    const img = element("img");
    img.alt = title;
    img.loading = detail ? "eager" : "lazy";
    img.decoding = "async";
    img.addEventListener("error", () => {
      img.hidden = true;
      placeholder.hidden = false;
    });
    img.src = mediaUrl(item.digest, detail ? "cover" : "thumbnail");
    frame.append(img);
  }
  return frame;
}

function metadataLine(values) {
  const line = element("div", "meta-line");
  for (const [text, chip] of values) {
    if (text) line.append(element("span", chip ? "chip" : "", text));
  }
  return line;
}

function favoriteButton(item, kind, refresh) {
  const id = kind === "style" ? item.style_key : item.digest;
  const control = button(item.favorite ? "★" : "☆", async () => {
    control.disabled = true;
    try {
      const next = !item.favorite;
      const saved = await api("/api/edit", {kind, key: id, favorite: next});
      item.favorite = saved.favorite;
      control.textContent = saved.favorite ? "★" : "☆";
      control.setAttribute("aria-pressed", String(Boolean(saved.favorite)));
      control.setAttribute("aria-label", saved.favorite ? "取消收藏" : "收藏");
      await refresh();
    } finally {
      control.disabled = false;
    }
  }, "soft fav");
  control.setAttribute("aria-pressed", String(Boolean(item.favorite)));
  control.setAttribute("aria-label", item.favorite ? "取消收藏" : "收藏");
  return control;
}

function card(item, style) {
  const title = style ? styleTitle(item) : imageTitle(item);
  const node = element("article", "card");
  const main = button("", () => style ? openStyle(item.style_key, $("model").value) : openImage(item.digest), "card-main");
  const caption = element("div", "card-caption");
  const model = item.model_version || item.model.replace(/^NovelAI Diffusion /, "NAI ");
  caption.append(element("strong", "", title), metadataLine(style
    ? [[model, true], ["命中 " + item.matches + " 张", false], [item.pinned === item.digest ? "封面已固定" : "", false]]
    : [[model, true], [!item.present ? "原图缺失" : !item.has_prompt ? "待补词" : "", false]]));
  main.append(picture(item, title), caption);
  const controls = element("div", "card-actions");
  if (style) controls.append(button("复制画师串", () => copyText(item.artist_text)));
  else if (item.has_prompt) controls.append(button("复制正向", async () => {
    const full = await api("/api/images/" + item.digest);
    await copyText(full.positive);
  }));
  controls.append(favoriteButton(item, style ? "style" : "image", loadList));
  node.append(main, controls);
  return node;
}

function setResultEmpty(title, description, action) {
  const empty = $("empty");
  empty.replaceChildren(element("div", "empty-mark", state.view === "styles" ? "◈" : "▦"),
    element("h2", "", title), element("p", "", description));
  if (action) empty.append(action);
  empty.hidden = false;
}

function createFeed(cards, footer, {read, render, key, root = null, announce = () => {}, draw = () => {}}) {
  let loaded = new Map();
  let nextPage = 1;
  let finished = false;
  let stopped = false;
  let request;
  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) void load();
  }, {root, rootMargin: "0px 0px 480px"});

  async function load(refresh = false) {
    if (stopped || (!refresh && (request || finished))) return;
    request?.abort();
    const current = new AbortController();
    request = current;
    observer.unobserve(footer);
    footer.hidden = false;
    footer.textContent = "正在加载…";
    try {
      const incoming = new Map();
      const last = refresh ? Math.max(1, nextPage - 1) : nextPage;
      let result;
      for (let page = refresh ? 1 : nextPage; page <= last; page++) {
        result = await read(page, current.signal);
        if (current.signal.aborted) return;
        for (const item of result.items) incoming.set(key(item), item);
        if (result.page * result.page_size >= result.total) break;
      }

      const fragment = document.createDocumentFragment();
      const scrollport = root || document.scrollingElement;
      const top = scrollport.scrollTop;
      const updated = refresh ? new Map() : loaded;
      for (const [id, item] of incoming) {
        const previous = loaded.get(id);
        if (!refresh && previous) continue;
        const signature = JSON.stringify(item);
        const node = previous?.signature === signature ? previous.node : render(item);
        fragment.append(node);
        updated.set(id, {node, signature});
      }
      if (refresh) cards.replaceChildren(fragment);
      else cards.append(fragment);
      loaded = updated;
      nextPage = result.page + 1;
      finished = result.page * result.page_size >= result.total;
      draw();
      announce(result.total);
      if (refresh) scrollport.scrollTop = top;
      footer.textContent = finished ? "已显示全部结果" : "";
      footer.hidden = result.total === 0;
      if (!finished) observer.observe(footer);
    } catch (error) {
      if (current.signal.aborted) return;
      footer.replaceChildren(document.createTextNode("加载失败"), button("重试", () => load(refresh)));
    } finally {
      if (request === current) request = null;
    }
  }

  return {
    start: () => load(),
    refresh: () => load(true),
    stop() {
      stopped = true;
      request?.abort();
      observer.disconnect();
    }
  };
}

function layoutCards() {
  const cards = $("cards");
  const width = cards.clientWidth;
  measuredWidth = width;
  if (!cards.childElementCount || !width) {
    cards.style.height = "0px";
    return;
  }
  const style = getComputedStyle(cards);
  const gap = parseFloat(style.getPropertyValue("--card-gap"));
  const minimum = parseFloat(style.getPropertyValue("--card-min"));
  const columns = Math.max(1, Math.floor((width + gap) / (minimum + gap)));
  const tile = (width - gap * (columns - 1)) / columns;
  cards.style.setProperty("--card-width", tile + "px");
  const heights = new Array(columns).fill(0);
  const items = Array.from(cards.children, (node) => [node, node.offsetHeight]);
  for (const [node, height] of items) {
    const top = Math.min(...heights);
    const column = heights.indexOf(top);
    node.style.transform = "translate(" + column * (tile + gap) + "px," + top + "px)";
    heights[column] += height + gap;
  }
  cards.style.height = Math.max(...heights) - gap + "px";
}

function searchFilters() {
  return {q: $("query").value.trim(), model: $("model").value, mode: $("filter").value};
}

function announceResults(total, style, {q, model, mode}) {
  $("result-count").textContent = total + (style ? " 组画师串" : " 张图片");
  $("empty").hidden = true;
  if (total) return;
  if (q || model || mode) {
    setResultEmpty("没有匹配结果", "试试换个关键词或调整筛选。");
  } else if (!state.source.originals) {
    setResultEmpty("选择一个原图目录", "可以直接使用拾集的图片目录。",
      button("设置图片目录", showSource, "primary"));
  } else if (state.counts.images && style) {
    setResultEmpty("还没找到画师串", "画师串来自 NovelAI 原图正向提示词里的 artist: 标签。");
  } else {
    const busy = state.progress.phase === "running";
    setResultEmpty(busy ? "正在读取原图" : "图鉴还没有入典图片",
      busy ? "完成后会显示扫描结果。" : "扫描已选目录，提取图片内嵌的提示词。",
      busy ? null : button("扫描新图", startScan, "primary"));
  }
}

async function loadGallery(page = galleryPage, scrollUp = false) {
  galleryRequest?.abort();
  const request = new AbortController();
  galleryRequest = request;
  const footer = $("feed-status");
  footer.textContent = "正在加载…";
  footer.hidden = false;
  const filters = searchFilters();
  try {
    const page_size = $("gallery-page-size").value;
    const result = await api("/api/images?" + new URLSearchParams({...filters, page, page_size}), null, request.signal);
    if (request.signal.aborted) return;
    galleryPage = result.page;
    $("cards").replaceChildren(...result.items.map((item) => card(item, false)));
    announceResults(result.total, false, filters);
    const pages = Math.max(1, Math.ceil(result.total / result.page_size));
    for (const nav of document.querySelectorAll("[data-gallery-pager]")) {
      nav.hidden = pages <= 1;
      nav.querySelector("[data-page-number]").textContent = galleryPage + " / " + pages;
      nav.querySelector('[data-page-step="-1"]').disabled = galleryPage <= 1;
      nav.querySelector('[data-page-step="1"]').disabled = galleryPage >= pages;
      const field = nav.querySelector("input");
      field.max = pages;
      field.value = "";
    }
    footer.hidden = true;
    if (scrollUp) window.scrollTo(0, 0);
  } catch {
    if (request.signal.aborted) return;
    footer.replaceChildren(document.createTextNode("加载失败"), button("重试", () => loadGallery(page, scrollUp)));
  } finally {
    if (galleryRequest === request) galleryRequest = undefined;
  }
}

function resetList() {
  clearTimeout(searchTimer);
  galleryRequest?.abort();
  mainFeed?.stop();
  mainFeed = undefined;
  const cards = $("cards");
  const style = state.view === "styles";
  cards.classList.toggle("masonry", style);
  $("gallery-size-control").hidden = style;
  cards.replaceChildren();
  cards.style.height = "";
  window.scrollTo(0, 0);
  $("empty").hidden = true;
  $("result-count").textContent = "";

  for (const nav of document.querySelectorAll("[data-gallery-pager]")) nav.hidden = true;
  if (!style) {
    galleryPage = 1;
    return loadGallery();
  }
  const filters = searchFilters();
  mainFeed = createFeed(cards, $("feed-status"), {
    read: (page, signal) => api("/api/styles?" + new URLSearchParams({...filters, page}), null, signal),
    render: (item) => card(item, true),
    key: (item) => item.style_key,
    draw: layoutCards,
    announce: (total) => announceResults(total, true, filters)
  });
  return mainFeed.start();
}

function loadList() {
  if (state.view === "gallery") return loadGallery();
  return mainFeed ? mainFeed.refresh() : resetList();
}

function refreshNavigation(data) {
  Object.assign(state, data);
  const connected = data.source_available;
  $("source-status").classList.toggle("connected", connected);
  $("source-status").textContent = connected ? "图片目录已连接" : state.source.originals ? "图片目录不可用" : "等待配置图片目录";
  $("gallery-count").textContent = state.counts.images;
  $("style-count").textContent = state.counts.styles;
  const oldModel = $("model").value;
  const allModels = state.view === "styles" ? "全部 NAI" : "不限模型";
  $("model").replaceChildren(new Option(allModels, ""), ...data.models.map((model) => new Option(model, model)));
  $("model").value = data.models.includes(oldModel) ? oldModel : "";
  const picker = $("gallery-page-size");
  const oldSize = picker.value;
  const sizes = (data.gallery_page_sizes || [24]).map(String);
  picker.replaceChildren(...sizes.map((size) => new Option(size + " 张", size)));
  if (sizes.includes(oldSize)) picker.value = oldSize;
  const info = $("scan-status");
  const s = state.progress;
  $("scan").disabled = s.phase === "running";
  $("scan").textContent = s.phase === "running" ? "正在扫描…" : "⟳ 扫描新图";
  info.hidden = s.phase === "idle" || !s.phase;
  info.classList.toggle("error", s.phase === "error");
  if (s.phase === "running") info.textContent = "正在扫描 · 已查看 " + s.seen + " 张 · 新增 " + s.added + " 张";
  if (s.phase === "done") {
    info.textContent = "扫描完成 · 新增 " + s.added + " 张 · 跳过 " + s.skipped + " 张" +
      (s.failed ? " · " + s.failed + " 张暂无法识别，下次可重试" : "") +
      (s.missing ? " · " + s.missing + " 个文件已移走或缺失" : "");
  }
  if (s.phase === "error") info.textContent = s.message;
}

async function refreshState() {
  refreshNavigation(await api("/api/state"));
}

async function pollScan() {
  clearTimeout(scanTimer);
  try {
    await refreshState();
    if (state.progress.phase === "running") scanTimer = setTimeout(pollScan, 900);
    else await loadList();
  } catch (error) {
    $("scan").disabled = false;
    toast(error.message);
  }
}

async function startScan() {
  if (!state.source.originals) {
    showSource();
    return;
  }
  const key = $("scan");
  key.disabled = true;
  try {
    await api("/api/scan", {});
    await pollScan();
  } catch (error) {
    key.disabled = false;
    toast(error.message);
  }
}

function renderLayout() {
  document.body.classList.toggle("rail-collapsed", railCollapsed);
  const toggle = $("rail-toggle");
  const label = railCollapsed ? "展开导航栏" : "收起导航栏";
  toggle.setAttribute("aria-expanded", String(!railCollapsed));
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
}

function switchView(view) {
  state.view = view;
  const style = view === "styles";
  $("query").value = "";
  $("filter").value = "";
  $("filter").querySelector('option[value="pending"]').hidden = style;
  $("model").options[0].textContent = style ? "全部 NAI" : "不限模型";
  $("page-title").textContent = style ? "画师串" : "图鉴";
  $("page-summary").textContent = style ? "每组画师串显示一张代表图。" : "按提示词查找本机作品。";
  $("query").placeholder = style ? "搜画师名或画师串，多个条件用逗号隔开" : "搜提示词，多个条件用逗号隔开";
  $("query").setAttribute("aria-label", style ? "搜索画师串" : "搜索图鉴");
  for (const tab of document.querySelectorAll("[data-view]")) {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("current", selected);
    if (selected) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  void resetList();
}

function showSource() {
  const form = $("source-form");
  const locked = Boolean(state.source_locked);
  for (const name of ["originals", "thumbnails", "previews"]) {
    const field = form.elements.namedItem(name);
    field.value = state.source[name] || "";
    field.readOnly = locked;
  }
  form.querySelector('button[type="submit"]').hidden = locked;
  $("source-help").textContent = locked ? "图片目录由部署配置管理，法典从这里读取原图和预览。" :
    "填这台电脑上的完整路径。索引保存到法典，图片从原目录读取。";
  $("source-message").textContent = "";
  if (!$("source-dialog").open) $("source-dialog").showModal();
}

function promptBox(title, value) {
  if (!value) return null;
  const node = element("section", "prompt-pane");
  const heading = element("div", "prompt-head");
  heading.append(element("strong", "", title), button("复制", () => copyText(value)));
  node.append(heading, element("pre", "", value));
  return node;
}

function appendPrompt(target, title, value) {
  const box = promptBox(title, value);
  if (box) target.append(box);
}

function appendRecipe(target, item, showSettings = false) {
  appendPrompt(target, "正向提示词", item.positive);
  appendPrompt(target, "负面提示词", item.negative);
  if (item.characters.length) {
    const roles = element("details");
    roles.append(element("summary", "", "角色提示词"));
    item.characters.forEach((role, index) => {
      const number = "角色 " + (index + 1);
      appendPrompt(roles, number + " · 正向", role["正向"]);
      appendPrompt(roles, number + " · 负面", role["负面"]);
      if (role["位置"]?.length) appendPrompt(roles, number + " · 位置", JSON.stringify(role["位置"]));
    });
    target.append(roles);
  }
  const parameters = Object.keys(item.parameters).length ? JSON.stringify(item.parameters, null, 2) : "";
  const original = item.raw_positive !== item.positive ? item.raw_positive : "";
  if (showSettings) {
    appendPrompt(target, "生图参数", parameters);
    if (!parameters) target.append(element("p", "muted", "这张封面图暂无生图参数。"));
  }
  if (original || (parameters && !showSettings)) {
    const settings = element("details");
    settings.append(element("summary", "", "原始参数"));
    if (!showSettings) appendPrompt(settings, "生图参数", parameters);
    appendPrompt(settings, "原始正向字段", original);
    target.append(settings);
  }
}

function askRemoval(kind, key) {
  pendingRemoval = {kind, key};
  const isStyle = kind === "style";
  $("remove-title").textContent = isStyle ? "删除画师串" : "删除图片";
  $("remove-description").textContent = isStyle
    ? "这组画师串会移到法典回收站。同组图片仍可在图鉴查看。"
    : "这张图片会从法典图鉴和同组例图移到回收站。共享原图和缩略图仍保存在原目录。";
  $("remove-dialog").showModal();
}

async function removeEntry(event) {
  event.preventDefault();
  if (!pendingRemoval) return;
  const submission = pendingRemoval;
  const confirm = event.currentTarget.querySelector('button[type="submit"]');
  const request = detailRequest;
  confirm.disabled = true;
  try {
    await api("/api/trash", {...submission, trashed: true});
    $("remove-dialog").close();
    if (request === detailRequest && $("item-dialog").open) $("item-dialog").close();
    await Promise.all([refreshState(), loadList()]);
    toast(submission.kind === "style" ? "画师串已移到回收站" : "图片已移到回收站");
  } catch (error) {
    toast(error.message);
  } finally {
    confirm.disabled = false;
  }
}

function showTrash(kind = "image") {
  trashFeed?.stop();
  const dialog = $("trash-dialog");
  const holder = $("trash-items");
  const blank = element("p", "muted");
  blank.hidden = true;
  const cards = element("div", "cards");
  const footer = element("div", "feed-status");
  footer.setAttribute("role", "status");
  holder.replaceChildren(blank, cards, footer);
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  for (const tab of dialog.querySelectorAll("[data-trash-kind]")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.trashKind === kind));
  }
  const style = kind === "style";
  const itemKey = (item) => style ? item.style_key : item.digest;
  const feed = createFeed(cards, footer, {
    root: dialog,
    read: (page, signal) => api("/api/trash?" + new URLSearchParams({kind, page}), null, signal),
    key: itemKey,
    announce(total) {
      blank.hidden = total > 0;
      blank.textContent = style ? "暂无已删除的画师串。" : "暂无已删除的图片。";
    },
    render(item) {
      const title = style ? styleTitle(item) : imageTitle(item);
      const tile = element("article", "card");
      const caption = element("div", "card-caption");
      caption.append(element("strong", "", title));
      const controls = element("div", "card-actions");
      const restore = button(style ? "恢复画师串" : "恢复图片", async () => {
        restore.disabled = true;
        try {
          await api("/api/trash", {kind, key: itemKey(item), trashed: false});
          await Promise.all([refreshState(), loadList()]);
          if (trashFeed === feed && dialog.open) await feed.refresh();
          toast(style ? "已恢复画师串" : "已恢复图片");
        } finally {
          restore.disabled = false;
        }
      });
      controls.append(restore);
      tile.append(picture(item, title), caption, controls);
      return tile;
    }
  });
  trashFeed = feed;
  return feed.start();
}

function editing(item, kind) {
  const key = kind === "style" ? item.style_key : item.digest;
  const section = element("details");
  const summary = element("summary", "", "名称与笔记");
  const form = element("form", "edit");
  const title = element("input");
  title.value = item.title || "";
  if (kind === "style") title.placeholder = item.default_title || "";
  title.maxLength = 100;
  const notes = element("textarea");
  notes.value = item.notes || "";
  notes.maxLength = 4000;
  const titleLabel = element("label", "", "名称");
  titleLabel.append(title);
  const notesLabel = element("label", "", "笔记");
  notesLabel.append(notes);
  const save = element("button", "soft", "保存笔记");
  save.type = "submit";
  form.append(titleLabel, notesLabel, save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      const saved = await api("/api/edit", {kind, key, title: title.value, notes: notes.value});
      Object.assign(item, saved);
      title.value = saved.title;
      notes.value = saved.notes;
      $("item-title").textContent = kind === "style" ? styleTitle(item) : imageTitle(item);
      toast("笔记已保存");
      await loadList();
    } catch (error) {
      toast(error.message);
    } finally {
      save.disabled = false;
    }
  });
  section.append(summary, form);
  return section;
}

function beginDetail(title) {
  coverFeed?.stop();
  $("item-title").textContent = title;
  $("item-body").replaceChildren(element("p", "muted", "正在读取…"));
  if (!$("item-dialog").open) $("item-dialog").showModal();
  return ++detailRequest;
}

function detailError(request, error) {
  if (request === detailRequest && $("item-dialog").open) {
    $("item-body").replaceChildren(element("p", "error-text", error.message));
  }
}

async function openImage(digest) {
  const request = beginDetail("作品详情");
  try {
    const item = await api("/api/images/" + digest);
    if (request !== detailRequest || !$("item-dialog").open) return;
    const title = imageTitle(item);
    $("item-title").textContent = title;
    const layout = element("div", "detail");
    const visual = element("div");
    const controls = element("div", "detail-control");
    visual.append(picture(item, title, true), controls);
    if (item.present) controls.append(link("查看原图", mediaUrl(digest, "original")),
      link("下载原图", mediaUrl(digest, "original") + "?download=1"));
    controls.append(favoriteButton(item, "image", loadList),
      button("删除图片", () => askRemoval("image", item.digest), "soft danger"));
    const info = element("div", "detail-information");
    info.append(metadataLine([[item.model, true], [item.tool, false], [item.width + " × " + item.height, false]]));
    if (!item.has_prompt) info.append(element("p", "muted", "这张原图待补词。"));
    appendRecipe(info, item);
    if (item.style_key) {
      appendPrompt(info, "画师串", item.artist_text);
      if (!item.style_removed) info.append(button("查看这一组画师串", () => openStyle(item.style_key, "")));
    }
    info.append(editing(item, "image"));
    layout.append(visual, info);
    $("item-body").replaceChildren(layout);
  } catch (error) {
    detailError(request, error);
  }
}

async function saveCover(key, digest, model, choose = false) {
  const request = beginDetail("画师串详情");
  try {
    await api("/api/edit", {kind: "style", key, cover: digest});
    const list = loadList();
    if (request === detailRequest && $("item-dialog").open) {
      await openStyle(key, model, choose);
      toast(digest ? "已固定封面" : "已恢复自动封面");
    }
    await list;
  } catch (error) {
    detailError(request, error);
  }
}

async function openStyle(key, model = "", choose = false) {
  const request = beginDetail("画师串详情");
  try {
    const query = new URLSearchParams({model});
    const data = await api("/api/styles/" + key + "?" + query);
    if (request !== detailRequest || !$("item-dialog").open) return;
    const item = data.entry;
    const title = styleTitle(item);
    $("item-title").textContent = title;
    const layout = element("div", "detail");
    const visual = element("div");
    visual.append(picture(item, title, true));
    const photoControls = element("div", "detail-control");
    photoControls.append(button("查看这张作品", () => openImage(item.digest)),
      button("删除这张图片", () => askRemoval("image", item.digest), "soft danger"));
    visual.append(photoControls);
    const info = element("div", "detail-information");
    info.append(metadataLine([[item.model_version || item.model, true], ["命中 " + item.matches + " 张", false],
      [item.pinned === item.digest ? "封面已固定" : "自动封面", false]]));
    if (model || data.models.length > 1) {
      const namedModels = element("select");
      namedModels.setAttribute("aria-label", "例图模型");
      namedModels.append(new Option("全部 NAI", ""), ...data.models.map(
        (row) => new Option(row.model + "（" + row.matches + " 张）", row.model)));
      namedModels.value = model;
      namedModels.addEventListener("change", () => openStyle(key, namedModels.value, choose));
      info.append(namedModels);
    }
    appendPrompt(info, "画师串", item.artist_text);
    const controls = element("div", "detail-control");
    controls.append(button(choose ? "收起同组图片" : "挑选封面", () => openStyle(key, model, !choose)), favoriteButton(item, "style", loadList));
    if (item.pinned) controls.append(button("取消固定封面", () => saveCover(key, "", model, choose)));
    controls.append(button("删除画师串", () => askRemoval("style", key), "soft danger"));
    info.append(controls);
    appendRecipe(info, data.recipe, true);
    info.append(editing(item, "style"));
    layout.append(visual, info);
    $("item-body").replaceChildren(layout);
    if (choose) {
      const section = element("section", "candidates");
      const cards = element("div", "cards");
      const footer = element("div", "feed-status");
      footer.setAttribute("role", "status");
      section.append(element("h3", "", "同组图片"), cards, footer);
      $("item-body").append(section);
      coverFeed = createFeed(cards, footer, {
        root: $("item-dialog"),
        read: (page, signal) => api("/api/images?" + new URLSearchParams({style: key, model, page}), null, signal),
        key: (candidate) => candidate.digest,
        render(candidate) {
          const tile = element("article", "card");
          const actions = element("div", "card-actions");
          const chooseCover = button(candidate.digest === item.pinned ? "已固定" : "设为封面", () => saveCover(key, candidate.digest, model));
          chooseCover.disabled = !candidate.present || candidate.digest === item.pinned;
          actions.append(chooseCover);
          const meta = element("div", "card-caption");
          meta.append(metadataLine([[candidate.width + " × " + candidate.height, false]]));
          tile.append(picture(candidate, imageTitle(candidate)), meta, actions);
          return tile;
        }
      });
      void coverFeed.start();
    }
  } catch (error) {
    detailError(request, error);
  }
}

document.querySelectorAll("[data-view]").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
document.querySelectorAll("[data-close]").forEach((close) => close.addEventListener("click", () => $(close.dataset.close).close()));
$("item-dialog").addEventListener("close", () => {
  if ($("item-dialog").open) return;
  detailRequest += 1;
  coverFeed?.stop();
  $("toast").hidden = true;
});
$("trash-dialog").addEventListener("close", () => {
  if (!$("trash-dialog").open) trashFeed?.stop();
});
$("open-source").addEventListener("click", showSource);
$("rail-toggle").addEventListener("click", () => {
  railCollapsed = !railCollapsed;
  renderLayout();
  try { localStorage.setItem("grimoire.railCollapsed", railCollapsed ? "1" : "0"); } catch { /* The control remains usable. */ }
});
$("scan").addEventListener("click", startScan);
new ResizeObserver(([entry]) => {
  if (state.view === "styles" && Math.round(entry.contentRect.width) !== measuredWidth) layoutCards();
}).observe($("cards"));
for (const nav of document.querySelectorAll("[data-gallery-pager]")) {
  for (const control of nav.querySelectorAll("[data-page-step]")) {
    control.addEventListener("click", () => loadGallery(galleryPage + Number(control.dataset.pageStep), true));
  }
  nav.querySelector("form").addEventListener("submit", (event) => {
    event.preventDefault();
    void loadGallery(nav.querySelector("input").valueAsNumber, true);
  });
}
$("open-trash").addEventListener("click", () => showTrash());
for (const tab of document.querySelectorAll("[data-trash-kind]")) {
  tab.addEventListener("click", () => showTrash(tab.dataset.trashKind));
}
$("remove-form").addEventListener("submit", removeEntry);
$("model").addEventListener("change", resetList);
$("filter").addEventListener("change", resetList);
$("gallery-page-size").addEventListener("change", (event) => {
  try { localStorage.setItem("grimoire.galleryPageSize", event.target.value); } catch { /* The selection still works. */ }
  resetList();
});
$("query").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(resetList, 180);
});
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(searchTimer);
  resetList();
});
$("source-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const save = event.currentTarget.querySelector('button[type="submit"]');
  save.disabled = true;
  try {
    await api("/api/source", Object.fromEntries(new FormData(event.currentTarget)));
    $("source-dialog").close();
    await refreshState();
    resetList();
    toast("图片目录已保存，可以开始扫描");
  } catch (error) {
    $("source-message").textContent = error.message;
    $("source-message").className = "error-text";
  } finally {
    save.disabled = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !document.querySelector("dialog[open]") && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) {
    event.preventDefault();
    $("query").focus();
  }
});

async function initialize() {
  try {
    railCollapsed = localStorage.getItem("grimoire.railCollapsed") === "1";
  } catch { /* Keep the default. */ }
  renderLayout();
  try {
    await refreshState();
  } catch (error) {
    toast(error.message);
  }
  try {
    const savedSize = localStorage.getItem("grimoire.galleryPageSize");
    const picker = $("gallery-page-size");
    if ([...picker.options].some((option) => option.value === savedSize)) picker.value = savedSize;
  } catch { /* Use the default size. */ }
  if (state.progress.phase === "running") void pollScan();
  await resetList();
}
void initialize();
