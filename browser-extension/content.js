// 完美世界电竞开放平台的新闻编辑页（新闻内容 > XX 新闻详细）右下角挂一个面板：拖入投稿包，一键填写。
// 跑在页面自己的 JS 环境里（manifest 里 world: MAIN），才能拿到 Quill 编辑器实例。
// 只填写、只上传图片，不点「提交」：填完由人检查后自己提交。
(() => {
  if (window.__wmpvpSubmitHelper) return;
  window.__wmpvpSubmitHelper = true;

  // 只在开放平台的页面上工作（index.html 里的 noscript 文案），域名变了也不用改插件
  const noscript = document.querySelector("noscript");
  if (!noscript || !noscript.textContent.includes("完美世界电竞开放平台")) return;

  const FORMAT = "wmpvp-news";
  const COVER_SIZE = [702, 300];
  const SMALL_COVER_SIZE = [184, 138];
  const COVER_FOCUS_Y = 0.4; // 裁成横幅时偏上一点，人脸一般在画面上半部分
  const UPLOAD_TIMEOUT = 60000;
  const GAMES = { csgo: "CSGO", dota2: "DOTA2", deadlock: "Deadlock" };

  // ── 工具 ──────────────────────────────────────────────────────────────────

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitFor(check, timeout, interval = 200) {
    const deadline = Date.now() + timeout;
    for (;;) {
      const value = check();
      if (value) return value;
      if (Date.now() > deadline) return null;
      await sleep(interval);
    }
  }

  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const isRemote = (img) => /^https?:/.test(img.getAttribute("src") || "");

  // Vue 2 的 v-model 听的是原生 input 事件；要用原型上的 setter 赋值，再派发事件
  function setInputValue(input, value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Element UI 的 el-upload 在 input[type=file] 的 change 里读 files 并上传
  function chooseFile(input, file) {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function cropTo(blob, [width, height], name) {
    const bitmap = await createImageBitmap(blob);
    const scale = Math.max(width / bitmap.width, height / bitmap.height);
    const sw = width / scale, sh = height / scale;
    const sx = (bitmap.width - sw) / 2;
    const sy = Math.min(Math.max(0, bitmap.height * COVER_FOCUS_Y - sh / 2), bitmap.height - sh);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").drawImage(bitmap, sx, sy, sw, sh, 0, 0, width, height);
    const out = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    return new File([out], name, { type: "image/jpeg" });
  }

  // ── 投稿包 ────────────────────────────────────────────────────────────────

  // 返回 {article, base, covers: [{file, label, blob, url}], images: Map<路径, Blob>}
  async function loadPackage(file) {
    const entries = await globalThis.__wmpvpReadZip(file);
    const jsonPath = [...entries.keys()].filter((name) => name === "article.json" || name.endsWith("/article.json"))
      .sort((a, b) => a.length - b.length)[0];
    if (!jsonPath) throw new Error("压缩包里没有 article.json，不是投稿包");
    const base = jsonPath.slice(0, -"article.json".length);
    const article = JSON.parse(await (await entries.get(jsonPath)()).text());
    if (article.format !== FORMAT) throw new Error(`article.json 的 format 不是 ${FORMAT}`);
    if (article.version !== 1) throw new Error(`不认识的投稿包版本：${article.version}，请更新插件`);
    if (!article.title || !Array.isArray(article.body)) throw new Error("article.json 缺少标题或正文");

    const images = new Map();
    const load = async (path) => {
      if (!images.has(path)) {
        const entry = entries.get(base + path);
        if (!entry) throw new Error(`压缩包里没有图片 ${path}`);
        images.set(path, await entry());
      }
      return images.get(path);
    };
    const covers = [];
    for (const cover of article.covers || []) {
      const blob = await load(cover.file);
      covers.push({ ...cover, blob, url: URL.createObjectURL(blob) });
    }
    for (const block of article.body) {
      if (block.type === "image" && block.file) await load(block.file);
    }
    return { article, covers, images, name: file.name };
  }

  // ── 填写 ──────────────────────────────────────────────────────────────────

  function form() {
    const root = document.querySelector(".g-addnews");
    if (!root) return null;
    const title = root.querySelector('input[placeholder="请输入标题"]');
    const summary = root.querySelector('input[placeholder="请输入摘要"]');
    const container = root.querySelector(".quill-editor .ql-container");
    const quill = (container && container.__quill) || root.querySelector(".quill-editor")?.__vue__?.quill;
    if (!title || !summary || !quill) return null;
    const bigCover = root.querySelector(".m-imguploader:not(.supload)");
    const smallCover = root.querySelector(".m-imguploader.supload");
    return {
      root, title, summary, quill, bigCover, smallCover,
      bodyUpload: root.querySelector(".quill-img-uploader input[type=file]"),
    };
  }

  function pageGame() {
    const crumb = document.querySelector(".app-breadcrumb")?.textContent || "";
    return Object.keys(GAMES).find((key) => crumb.includes(GAMES[key]));
  }

  function hasContent(f) {
    return !!(f.title.value || f.summary.value || f.quill.getText().trim() ||
      f.quill.root.querySelector("img") || f.bigCover?.querySelector("img"));
  }

  // 正文 → Quill Delta：Q 整段加粗，A 和其他段落不加粗；图片先放一段占位文字，上传后换成图片
  function bodyDelta(blocks) {
    const ops = [];
    const tokens = [];
    for (const block of blocks) {
      if (block.type === "image") {
        const token = `【图片 ${tokens.length + 1} 上传中……如果一直停在这里，请在此处手动插入图片】`;
        tokens.push({ token, block });
        ops.push({ insert: token }, { insert: "\n" });
        continue;
      }
      for (const line of String(block.text || "").split(/\n+/).map((s) => s.trim()).filter(Boolean)) {
        ops.push(block.type === "q" ? { insert: line, attributes: { bold: true } } : { insert: line }, { insert: "\n" });
      }
    }
    return { ops, tokens };
  }

  // 文字在编辑器里的位置。不能用 getText()：它跳过图片，图片之后的位置会错
  function findText(quill, text) {
    let index = 0;
    for (const op of quill.getContents().ops) {
      if (typeof op.insert !== "string") { index += 1; continue; }
      const at = op.insert.indexOf(text);
      if (at >= 0) return index + at;
      index += op.insert.length;
    }
    return -1;
  }

  async function insertBodyImage(f, token, file, log) {
    const { quill } = f;
    const index = findText(quill, token);
    if (index < 0) throw new Error("找不到图片占位文字（被改动了？）");
    const before = new Set(quill.root.querySelectorAll("img"));
    quill.setSelection(index, 0, "user");
    chooseFile(f.bodyUpload, file);
    const img = await waitFor(() => [...quill.root.querySelectorAll("img")].find((i) => !before.has(i) && isRemote(i)),
      UPLOAD_TIMEOUT);
    if (!img) throw new Error("正文图片上传超时");
    // 网站把图片插在光标处，应该紧挨在占位文字前面；插到别处就挪回来
    const blot = quill.constructor.find(img);
    let at = findText(quill, token);
    if (!blot || quill.getIndex(blot) !== at - 1) {
      if (blot) quill.deleteText(quill.getIndex(blot), 1, "user");
      at = findText(quill, token);
      quill.insertEmbed(at, "image", img.getAttribute("src"), "user");
      at += 1;
      log("正文图片插错了位置，已挪回原处");
    }
    quill.deleteText(at, token.length, "user");
    quill.formatLine(at - 1, 1, "align", "center", "user");
  }

  async function uploadCover(f, blob, log) {
    if (!f.bigCover) throw new Error("页面上找不到大预览图的上传框");
    const card = f.bigCover.closest(".el-card") || f.root;
    const bigInput = f.bigCover.querySelector("input[type=file]");
    const before = f.bigCover.querySelector("img")?.getAttribute("src");
    chooseFile(bigInput, await cropTo(blob, COVER_SIZE, "cover_702x300.jpg"));
    // 网站可能弹「图片裁剪」：图已经是 702×300，直接确定
    const uploaded = await waitFor(() => {
      const dialog = [...document.querySelectorAll('.el-dialog[aria-label="图片裁剪"]')].find(visible);
      const confirm = dialog && [...dialog.querySelectorAll("button")].find((b) => b.textContent.includes("确定"));
      if (confirm) { confirm.click(); log("已确认图片裁剪"); }
      const img = f.bigCover.querySelector("img");
      return img && isRemote(img) && img.getAttribute("src") !== before ? img : null;
    }, UPLOAD_TIMEOUT, 300);
    if (!uploaded) throw new Error("大预览图上传超时");
    log("大预览图已上传");
    if (!f.smallCover) return;

    const sync = await waitFor(() => [...card.querySelectorAll("button")].find((b) => b.textContent.includes("同步") && visible(b)), 3000);
    const smallSrc = () => f.smallCover.querySelector("img")?.getAttribute("src");
    if (sync) {
      sync.click();
      if (await waitFor(() => smallSrc() === uploaded.getAttribute("src") || (smallSrc() && smallSrc() !== before), 10000)) {
        log("小预览图已同步");
        return;
      }
    }
    chooseFile(f.smallCover.querySelector("input[type=file]"), await cropTo(blob, SMALL_COVER_SIZE, "cover_184x138.jpg"));
    if (!(await waitFor(() => smallSrc() && isRemote(f.smallCover.querySelector("img")), UPLOAD_TIMEOUT))) {
      throw new Error("小预览图上传超时");
    }
    log("小预览图已上传");
  }

  // cover：选中的封面 Blob，null 表示不上传预览图、不插正文配图
  async function fill(pkg, cover, log) {
    const f = form();
    if (!f) throw new Error("没找到编辑表单，请打开新闻编辑页");
    const { article } = pkg;
    const game = pageGame();
    if (article.game && game && article.game !== game) {
      if (!confirm(`投稿包是 ${GAMES[article.game] || article.game} 的，当前页面是 ${GAMES[game]}，确定要填到这里吗？`)) return;
    }
    if (hasContent(f) && !confirm("页面上已经有内容，覆盖标题、摘要和正文吗？")) return;

    setInputValue(f.title, article.title);
    setInputValue(f.summary, article.summary || "");
    log("已填写标题和摘要");

    const blocks = article.body.filter((b) => b.type !== "image" || b.file || cover);
    const { ops, tokens } = bodyDelta(blocks);
    f.quill.setContents(ops, "user");
    log(`已填写正文（${blocks.length - tokens.length} 段）`);

    const errors = [];
    for (const [i, { token, block }] of tokens.entries()) {
      const blob = block.file ? pkg.images.get(block.file) : cover;
      const name = block.file ? block.file.split("/").pop() : "image.jpg";
      try {
        if (!f.bodyUpload) throw new Error("页面上找不到正文图片的上传框");
        await insertBodyImage(f, token, new File([blob], name, { type: blob.type || "image/jpeg" }), log);
        log(`正文图片 ${i + 1} 已插入`);
      } catch (error) {
        errors.push(`正文图片 ${i + 1}：${error.message}`);
      }
    }
    if (cover) {
      try {
        await uploadCover(f, cover, log);
      } catch (error) {
        errors.push(`预览图：${error.message}`);
      }
    }
    f.quill.setSelection(0, 0, "silent");
    f.root.scrollIntoView({ behavior: "smooth", block: "start" });
    if (errors.length) throw new Error(errors.join("；") + "。其余部分已填好，这些请手动处理");
    log("✅ 填写完成。TAG 没有填，请补上 TAG、检查无误后自己点「提交」");
  }

  // ── 面板 ──────────────────────────────────────────────────────────────────

  const host = document.createElement("div");
  host.id = "wmpvp-submit-helper";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  .pill { position: fixed; right: 24px; bottom: 24px; z-index: 2147483000; padding: 10px 16px; border-radius: 20px;
    background: #409eff; color: #fff; font-size: 14px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,.2); border: 0; }
  .panel { position: fixed; right: 24px; bottom: 24px; z-index: 2147483000; width: 380px; max-height: calc(100vh - 48px);
    overflow: auto; background: #fff; border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.25); font-size: 13px; color: #303133; }
  .head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #ebeef5; font-weight: 600; }
  .head button { border: 0; background: none; font-size: 18px; cursor: pointer; color: #909399; }
  .body { padding: 12px 16px; }
  .drop { border: 2px dashed #c0c4cc; border-radius: 6px; padding: 18px; text-align: center; color: #909399; cursor: pointer; }
  .drop.over { border-color: #409eff; color: #409eff; background: #ecf5ff; }
  .label { color: #909399; margin: 10px 0 4px; }
  .value { line-height: 1.5; word-break: break-all; }
  .warn { color: #e6a23c; margin-top: 6px; }
  .covers { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .cover { border: 2px solid transparent; border-radius: 4px; cursor: pointer; overflow: hidden; background: #f5f7fa;
    font-size: 11px; color: #909399; text-align: center; }
  .cover img { display: block; width: 100%; height: 60px; object-fit: cover; }
  .cover.none { display: flex; align-items: center; justify-content: center; min-height: 64px; }
  .cover.on { border-color: #409eff; }
  .fill { width: 100%; margin-top: 12px; padding: 10px; border: 0; border-radius: 4px; background: #67c23a; color: #fff; font-size: 14px; cursor: pointer; }
  .fill:disabled { background: #b3e19d; cursor: default; }
  .log { margin-top: 10px; max-height: 140px; overflow: auto; color: #606266; white-space: pre-wrap; }
  .log .error { color: #f56c6c; }
  [hidden] { display: none !important; }
</style>
<button class="pill" title="拖入投稿包 zip">投稿包</button>
<div class="panel" hidden>
  <div class="head"><span>投稿助手</span><button class="close" title="收起">×</button></div>
  <div class="body">
    <div class="drop">把投稿包 zip 拖到这里，或点击选择</div>
    <input class="file" type="file" accept=".zip,application/zip" hidden>
    <div class="info" hidden>
      <div class="label">文件</div><div class="value name"></div>
      <div class="label">标题</div><div class="value title"></div>
      <div class="label">摘要</div><div class="value summary"></div>
      <div class="label">正文</div><div class="value stats"></div>
      <div class="warnings"></div>
      <div class="label">预览图（同时用作正文配图）</div>
      <div class="covers"></div>
      <button class="fill">填写到页面</button>
    </div>
    <div class="log"></div>
  </div>
</div>`;

  const $ = (selector) => shadow.querySelector(selector);
  const pill = $(".pill"), panel = $(".panel"), drop = $(".drop"), fileInput = $(".file"), fillButton = $(".fill");
  let pkg = null;
  let chosen = 0; // 封面下标；-1 表示不用

  function log(text, error = false) {
    const line = document.createElement("div");
    line.textContent = text;
    if (error) line.className = "error";
    $(".log").appendChild(line);
    line.scrollIntoView({ block: "nearest" });
  }

  function open(show) {
    panel.hidden = !show;
    pill.hidden = show;
  }

  function renderCovers() {
    const box = $(".covers");
    box.textContent = "";
    pkg.covers.forEach((cover, index) => {
      const item = document.createElement("div");
      item.className = "cover" + (index === chosen ? " on" : "");
      item.title = cover.label || cover.file;
      const img = document.createElement("img");
      img.src = cover.url;
      item.append(img, cover.label || "");
      item.onclick = () => { chosen = index; renderCovers(); };
      box.appendChild(item);
    });
    const none = document.createElement("div");
    none.className = "cover none" + (chosen === -1 ? " on" : "");
    none.textContent = "不上传图片";
    none.onclick = () => { chosen = -1; renderCovers(); };
    box.appendChild(none);
  }

  async function openPackage(file) {
    if (!file) return;
    $(".log").textContent = "";
    try {
      if (pkg) pkg.covers.forEach((c) => URL.revokeObjectURL(c.url));
      pkg = await loadPackage(file);
    } catch (error) {
      pkg = null;
      $(".info").hidden = true;
      log(`读取失败：${error.message}`, true);
      return;
    }
    const { article } = pkg;
    chosen = pkg.covers.length ? Math.max(0, pkg.covers.findIndex((c) => c.file === article.cover)) : -1;
    $(".name").textContent = pkg.name + (article.game ? `（${GAMES[article.game] || article.game}）` : "");
    $(".title").textContent = article.title;
    $(".summary").textContent = article.summary || "（空）";
    const count = (type) => article.body.filter((b) => b.type === type).length;
    $(".stats").textContent = `${article.body.length - count("image")} 段，其中提问 ${count("q")} 个；配图 ${count("image")} 处`;
    const warnings = [...(article.warnings || [])];
    const game = pageGame();
    if (article.game && game && article.game !== game) warnings.push(`投稿包是 ${GAMES[article.game] || article.game} 的，当前页面是 ${GAMES[game]}`);
    $(".warnings").innerHTML = "";
    for (const text of warnings) {
      const line = document.createElement("div");
      line.className = "warn";
      line.textContent = "⚠️ " + text;
      $(".warnings").appendChild(line);
    }
    renderCovers();
    $(".info").hidden = false;
    log("已读取投稿包，选好预览图后点「填写到页面」");
  }

  pill.onclick = () => open(true);
  $(".close").onclick = () => open(false);
  drop.onclick = () => fileInput.click();
  fileInput.onchange = () => { openPackage(fileInput.files[0]); fileInput.value = ""; };
  for (const target of [drop, pill]) {
    target.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
    target.addEventListener("dragleave", () => drop.classList.remove("over"));
    target.addEventListener("drop", (event) => {
      event.preventDefault();
      drop.classList.remove("over");
      open(true);
      openPackage(event.dataTransfer.files[0]);
    });
  }
  fillButton.onclick = async () => {
    if (!pkg) return;
    fillButton.disabled = true;
    try {
      await fill(pkg, chosen >= 0 ? pkg.covers[chosen].blob : null, log);
    } catch (error) {
      log(`❌ ${error.message}`, true);
    } finally {
      fillButton.disabled = false;
    }
  };

  // 单页应用：编辑页出现时挂上面板，离开时拿掉
  let scheduled = false;
  function syncMount() {
    scheduled = false;
    const onEditor = !!document.querySelector(".g-addnews");
    if (onEditor && !host.isConnected) document.body.appendChild(host);
    if (!onEditor && host.isConnected) host.remove();
  }
  new MutationObserver(() => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(syncMount); }
  }).observe(document.body, { childList: true, subtree: true });
  syncMount();
})();
