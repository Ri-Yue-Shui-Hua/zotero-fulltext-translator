/* Zotero Fulltext Translator (Ollama) — 插件主逻辑
 *
 * 职责：菜单、读取设置、定位 PDF、拉起 Python 引擎(子进程)、展示进度、把译文 PDF 导入 Zotero。
 * 真正的"翻译 + 版式重建"在 engine/translate_pdf.py（PyMuPDF）。
 */

var FTT = {
  id: null,
  version: null,
  rootURI: null,
  PREF: "extensions.zotero.ftt.",
  TRANSLATED_PREFIX: "[中文] ",
  COMPARISON_PREFIX: "[原译对照] ",
  workDir: null,
  enginePath: null,
  _added: new Map(), // window -> [elements]
  _job: null,

  // ---------------------------------------------------------------- 基础
  getPref(key) {
    return Zotero.Prefs.get(this.PREF + key, true);
  },
  log(msg) {
    Zotero.debug("[FTT] " + msg);
  },
  alert(title, msg, win) {
    const w = win || Zotero.getMainWindow();
    Services.prompt.alert(w, title, msg);
  },

  // ---------------------------------------------------------------- 生命周期
  async onStartup({ id, version, rootURI }) {
    this.id = id;
    this.version = version;
    this.rootURI = rootURI;

    // 1) 先注册设置面板——它不依赖任何其他步骤，避免被别处的异常连累
    try {
      this.paneID = await Zotero.PreferencePanes.register({
        pluginID: id,
        src: rootURI + "prefs.xhtml",
        scripts: [rootURI + "prefs-pane.js"],
        label: "全文翻译 (Ollama)",
      });
      this.log("preference pane registered: " + this.paneID);
    } catch (e) {
      this.fail("注册设置面板失败", e);
    }

    // 2) 菜单
    try {
      for (const win of Zotero.getMainWindows()) {
        this.addToWindow(win);
      }
    } catch (e) {
      this.fail("添加菜单失败", e);
    }

    // 3) 引擎脚本：失败不致命，使用时会再试一次
    try {
      await this.deployEngine();
    } catch (e) {
      this.fail("释放翻译引擎失败（稍后使用时会重试）", e);
    }
    this.log("started v" + version);
  },

  fail(what, e) {
    const msg = "[FTT] " + what + ": " + (e && e.stack ? e.stack : e);
    Zotero.debug(msg);
    try {
      Zotero.logError(e instanceof Error ? e : new Error(msg));
    } catch (_) {
      /* ignore */
    }
  },

  onShutdown() {
    this.cancel();
    for (const win of Array.from(this._added.keys())) {
      this.removeFromWindow(win);
    }
  },

  /** xpi 里的文件不能被外部进程直接执行，所以启动时把引擎脚本释放到数据目录 */
  async deployEngine() {
    const dir = PathUtils.join(Zotero.DataDirectory.dir, "fulltext-translator");
    await IOUtils.makeDirectory(dir, { createAncestors: true });
    await IOUtils.makeDirectory(PathUtils.join(dir, "cache"), { createAncestors: true });
    const code = await this.readResource(this.rootURI + "engine/translate_pdf.py");
    this.workDir = dir;
    this.enginePath = PathUtils.join(dir, "translate_pdf.py");
    await IOUtils.writeUTF8(this.enginePath, code);
  },

  /** 读取插件包内资源。不能用 Zotero.File.getContentsFromURLAsync：对 jar: 地址会抛 nsIURI.username 错误 */
  async readResource(url) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return await resp.text();
    } catch (e1) {
      this.log("fetch failed for " + url + " (" + e1 + "), falling back to NetUtil");
      const { NetUtil } = ChromeUtils.importESModule("resource://gre/modules/NetUtil.sys.mjs");
      return await new Promise((resolve, reject) => {
        NetUtil.asyncFetch({ uri: url, loadUsingSystemPrincipal: true }, (stream, status) => {
          if (!Components.isSuccessCode(status)) {
            reject(new Error("无法读取插件资源 " + url + "（status " + status + "）"));
            return;
          }
          try {
            resolve(NetUtil.readInputStreamToString(stream, stream.available(), { charset: "utf-8" }));
          } catch (e2) {
            reject(e2);
          }
        });
      });
    }
  },

  // ---------------------------------------------------------------- 菜单
  addToWindow(win) {
    if (this._added.has(win)) return;
    const doc = win.document;
    const els = [];

    const mk = (id, label, handler) => {
      const mi = doc.createXULElement("menuitem");
      mi.id = id;
      mi.setAttribute("label", label);
      mi.addEventListener("command", handler);
      els.push(mi);
      return mi;
    };

    // 条目右键菜单
    const itemMenu = doc.getElementById("zotero-itemmenu");
    if (itemMenu) {
      const sep = doc.createXULElement("menuseparator");
      sep.id = "ftt-itemmenu-sep";
      els.push(sep);
      itemMenu.appendChild(sep);
      itemMenu.appendChild(
        mk("ftt-itemmenu-translate", "翻译全文为中文 PDF（Ollama）", () => this.run(win, "selection"))
      );
      itemMenu.appendChild(
        mk("ftt-itemmenu-compare", "并排查看原文与译文", () => this.openComparison(win, "selection"))
      );
      itemMenu.appendChild(
        mk("ftt-itemmenu-summary", "生成论文总结到笔记", () => this.summarize(win, "selection"))
      );
    }

    // 工具菜单：翻译当前打开的论文 / 取消 / 打开设置
    const tools = doc.getElementById("menu_ToolsPopup");
    if (tools) {
      const sep = doc.createXULElement("menuseparator");
      sep.id = "ftt-tools-sep";
      els.push(sep);
      tools.appendChild(sep);
      tools.appendChild(
        mk("ftt-tools-translate", "翻译当前论文全文（Ollama）", () => this.run(win, "reader"))
      );
      tools.appendChild(
        mk("ftt-tools-compare", "并排查看当前论文原文与译文", () => this.openComparison(win, "reader"))
      );
      tools.appendChild(
        mk("ftt-tools-summary", "生成当前论文总结到笔记", () => this.summarize(win, "reader"))
      );
      tools.appendChild(      mk("ftt-tools-cancel", "取消正在进行的插件任务", () => this.cancel()));
      tools.appendChild(
        mk("ftt-tools-prefs", "全文翻译设置…", () => Zotero.Utilities.Internal.openPreferences(this.paneID))
      );
    }

    this._added.set(win, els);
  },

  removeFromWindow(win) {
    const els = this._added.get(win) || [];
    for (const el of els) {
      try {
        el.remove();
      } catch (e) {
        /* 窗口已关闭 */
      }
    }
    this._added.delete(win);
  },

  // ---------------------------------------------------------------- 定位目标 PDF
  getReaderAttachment(win) {
    try {
      const tabs = win.Zotero_Tabs;
      if (!tabs || tabs.selectedType !== "reader") return null;
      const reader = Zotero.Reader.getByTabID(tabs.selectedID);
      if (!reader) return null;
      const item = Zotero.Items.get(reader.itemID);
      return item && item.isPDFAttachment() ? item : null;
    } catch (e) {
      this.log("getReaderAttachment: " + e);
      return null;
    }
  },

  isTranslated(att) {
    return (att.getField("title") || "").startsWith(this.TRANSLATED_PREFIX);
  },

  async pickPdf(item) {
    if (item.isPDFAttachment()) return item;
    if (!item.isRegularItem()) return null;
    const best = await item.getBestAttachment();
    if (best && best.isPDFAttachment() && !this.isTranslated(best)) return best;
    for (const id of item.getAttachments()) {
      const a = Zotero.Items.get(id);
      if (a && a.isPDFAttachment() && !this.isTranslated(a)) return a;
    }
    return null;
  },

  async collectTargets(win, prefer) {
    if (prefer === "reader") {
      const att = this.getReaderAttachment(win);
      if (att) return [att];
    }
    const out = [];
    for (const item of win.ZoteroPane.getSelectedItems() || []) {
      const pdf = await this.pickPdf(item);
      if (pdf && !out.includes(pdf)) out.push(pdf);
    }
    return out;
  },

  // ---------------------------------------------------------------- 入口
  async run(win, prefer) {
    if (this._job) {
      this.alert("全文翻译", "已有翻译任务正在进行。可在「工具 → 取消正在进行的全文翻译」中止。", win);
      return;
    }
    let targets;
    try {
      targets = await this.collectTargets(win, prefer);
    } catch (e) {
      this.alert("全文翻译", "无法确定要翻译的论文：" + e, win);
      return;
    }
    if (!targets.length) {
      this.alert(
        "全文翻译",
        "没有找到可翻译的 PDF。\n请在文库中选中带 PDF 附件的条目，或先打开一篇论文再使用「工具 → 翻译当前论文全文」。",
        win
      );
      return;
    }
    for (const att of targets) {
      const ok = await this.translateOne(att, win);
      if (ok === "cancelled") break;
    }
  },

  async findTranslationPair(item) {
    let original = null;
    let translated = null;
    if (item.isPDFAttachment()) {
      if (this.isTranslated(item)) translated = item;
      else original = item;
    } else if (item.isRegularItem()) {
      original = await this.pickPdf(item);
    }

    const parent = item.parentItemID
      ? Zotero.Items.get(item.parentItemID)
      : item.isRegularItem()
        ? item
        : null;
    if (!parent) return null;
    if (!original) original = await this.pickPdf(parent);

    const translations = [];
    for (const id of parent.getAttachments()) {
      const attachment = Zotero.Items.get(id);
      if (attachment && attachment.isPDFAttachment() && this.isTranslated(attachment)) {
        translations.push(attachment);
      }
    }
    if (translations.length) {
      translations.sort((a, b) => {
        const dateA = a.getField("dateAdded") || "";
        const dateB = b.getField("dateAdded") || "";
        return dateA.localeCompare(dateB) || a.id - b.id;
      });
      translated = translations[translations.length - 1];
    }
    if (!original || !translated) return null;
    return { original, translated };
  },

  async keepLatestGeneratedAttachment(parent, prefix, keepID) {
    const staleIDs = [];
    for (const id of parent.getAttachments()) {
      const attachment = Zotero.Items.get(id);
      if (
        attachment &&
        attachment.id !== keepID &&
        attachment.isPDFAttachment() &&
        (attachment.getField("title") || "").startsWith(prefix)
      ) {
        staleIDs.push(attachment.id);
      }
    }
    if (staleIDs.length) {
      await Zotero.Items.trashTx(staleIDs);
      this.log(`moved ${staleIDs.length} stale generated attachment(s) to Zotero trash: ${prefix}`);
    }
  },

  async ensureAttachmentFilename(attachment, expectedName) {
    let path = await attachment.getFilePathAsync();
    if (!path) {
      throw this.err("E_OUTPUT_PATH", "Zotero 已导入译文，但无法读取其存储文件路径。");
    }
    if (PathUtils.filename(path) !== expectedName) {
      const renamed = await attachment.renameAttachmentFile(expectedName, false, false);
      if (renamed !== true) {
        throw this.err(
          "E_OUTPUT_PATH",
          "Zotero 导入时更改了译文文件名，且无法恢复预期名称。\n预期：" +
            expectedName +
            "\n当前：" +
            PathUtils.filename(path)
        );
      }
      path = await attachment.getFilePathAsync();
    }
    if (!path || PathUtils.filename(path) !== expectedName) {
      throw this.err(
        "E_OUTPUT_PATH",
        "Zotero 存储的译文文件名与输出文件名不一致。\n预期：" +
          expectedName +
          "\n当前：" +
          (path ? PathUtils.filename(path) : "(无法读取)")
      );
    }
    return path;
  },

  async openComparison(win, prefer) {
    if (this._job) {
      this.alert("原文与译文", "已有插件任务正在进行。请等待完成或先取消当前任务。", win);
      return;
    }
    let cfgPath = null;
    let progressWindow = null;
    let prog = null;
    try {
      let targets = await this.collectTargets(win, prefer);
      if (!targets.length && prefer === "reader") {
        const current = this.getReaderAttachment(win);
        if (current) targets = [current];
      }
      if (targets.length !== 1) {
        throw new Error(targets.length ? "请一次只选择一篇论文。" : "请选中论文条目或打开一份论文 PDF。");
      }
      const pair = await this.findTranslationPair(targets[0]);
      if (!pair) {
        throw new Error("未找到对应译文 PDF。请先完成翻译，并将译文作为 Zotero 子附件导入。");
      }

      const parent = Zotero.Items.get(pair.original.parentItemID);
      if (!parent) throw new Error("无法找到论文条目，不能保存对照 PDF。");
      const title = parent.getDisplayTitle() || pair.original.getDisplayTitle() || "未命名论文";
      const originalPath = await pair.original.getFilePathAsync();
      const translatedPath = await pair.translated.getFilePathAsync();
      if (!originalPath || !translatedPath) {
        throw this.err("E_NOFILE", "原文或译文 PDF 文件不存在（可能尚未同步下载），请先在 Zotero 中打开对应 PDF。");
      }

      if (!this.enginePath) await this.deployEngine();
      const py = await this.findPython();
      const comparisonTitle = this.COMPARISON_PREFIX + title;
      let comparison = null;
      let outPath = null;
      for (const id of parent.getAttachments()) {
        const attachment = Zotero.Items.get(id);
        if (
          attachment &&
          attachment.isPDFAttachment() &&
          attachment.getField("title") === comparisonTitle &&
          attachment.attachmentLinkMode === Zotero.Attachments.LINK_MODE_LINKED_FILE
        ) {
          outPath = await attachment.getFilePathAsync();
          if (outPath) {
            comparison = attachment;
            break;
          }
        }
      }
      const outDirPref = (this.getPref("outputDir") || "").trim();
      const outDir = outDirPref || PathUtils.join(Zotero.DataDirectory.dir, "translated-pdfs");
      await IOUtils.makeDirectory(outDir, { createAncestors: true });
      if (!outPath) {
        const base = Zotero.File.getValidFileName(PathUtils.filename(originalPath).replace(/\.pdf$/i, ""));
        outPath = await this.uniquePath(outDir, base, ".comparison.pdf");
      }

      progressWindow = new Zotero.ProgressWindow({ closeOnClick: true });
      progressWindow.changeHeadline("原文与译文对照");
      prog = new progressWindow.ItemProgress(pair.original.getImageSrc ? pair.original.getImageSrc() : "", title);
      prog.setProgress(0);
      progressWindow.show();
      const job = { cancelled: false, proc: null };
      this._job = job;
      cfgPath = PathUtils.join(this.workDir, "compare-" + Date.now() + ".json");
      await IOUtils.writeUTF8(cfgPath, JSON.stringify({ input: originalPath, translated_input: translatedPath, output: outPath }));

      let doneEvent = null;
      let errEvent = null;
      const result = await this.runEngine(py, cfgPath, ["--compare"], job, (ev) => {
        if (ev.type === "progress") {
          prog.setProgress(Math.min(95, Math.round((ev.page / ev.total) * 95)));
          prog.setText(`${title} — ${ev.message}`);
        } else if (ev.type === "log") {
          this.log("engine: " + ev.message);
        } else if (ev.type === "done") {
          doneEvent = ev;
        } else if (ev.type === "error") {
          errEvent = ev;
        }
      });
      if (job.cancelled) {
        prog.setError();
        prog.setText(`${title} — 已取消`);
        progressWindow.startCloseTimer(4000);
        return;
      }
      if (errEvent) throw this.err(errEvent.code, errEvent.message, errEvent.trace);
      if (result.exitCode !== 0 || !doneEvent) {
        throw this.err("E_COMPARE", "对照 PDF 生成失败（exit " + result.exitCode + "）。\n" + result.tail.slice(-8).join("\n"));
      }

      if (!comparison) {
        comparison = await Zotero.Attachments.linkFromFile({
          file: outPath,
          parentItemID: parent.id,
          title: comparisonTitle,
          contentType: "application/pdf",
        });
      }
      await this.keepLatestGeneratedAttachment(parent, this.COMPARISON_PREFIX, comparison.id);
      await this.keepLatestGeneratedAttachment(parent, this.TRANSLATED_PREFIX, pair.translated.id);
      prog.setProgress(100);
      prog.setText(`${title} — 正在打开单窗口对照阅读器`);
      progressWindow.startCloseTimer(5000);
      await Zotero.Reader.open(comparison.id, null, { openInWindow: true });
    } catch (e) {
      this.log("openComparison failed: " + (e.stack || e));
      if (prog) {
        prog.setError();
        prog.setText("原文与译文对照 — 失败");
        progressWindow.startCloseTimer(12000);
      }
      this.alert("原文与译文", this.explain(e), win);
    } finally {
      this._job = null;
      if (cfgPath) {
        try {
          await IOUtils.remove(cfgPath, { ignoreAbsent: true });
        } catch (e) {
          this.log("temporary comparison config cleanup failed: " + e);
        }
      }
    }
  },

  async summarize(win, prefer) {
    if (this._job) {
      this.alert("论文总结", "已有任务正在进行。请等待完成或先取消当前任务。", win);
      return;
    }
    let targets;
    try {
      targets = await this.collectTargets(win, prefer);
    } catch (e) {
      this.alert("论文总结", "无法确定要总结的论文：" + e, win);
      return;
    }
    if (!targets.length) {
      this.alert("论文总结", "没有找到可总结的 PDF。请选中带 PDF 的论文条目或在阅读器中打开论文。", win);
      return;
    }
    if (targets.some((att) => !att.parentItemID)) {
      this.alert("论文总结", "请先将 PDF 添加到一个 Zotero 论文条目下，才能把总结保存为该条目的笔记。", win);
      return;
    }
    for (const att of targets) {
      await this.summarizeOne(att, win);
    }
  },

  async summarizeOne(att, win) {
    const parent = Zotero.Items.get(att.parentItemID);
    const title = (parent && parent.getDisplayTitle()) || att.getDisplayTitle() || "未命名论文";
    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    pw.changeHeadline("论文总结");
    const prog = new pw.ItemProgress(att.getImageSrc ? att.getImageSrc() : "", title);
    prog.setProgress(0);
    pw.show();

    const job = { cancelled: false, proc: null };
    this._job = job;
    let cfgPath = null;
    try {
      const inPath = await att.getFilePathAsync();
      if (!inPath) {
        throw this.err("E_NOFILE", "附件文件不存在（可能尚未同步下载），请先在 Zotero 中打开一次该 PDF。");
      }
      if (!this.enginePath) await this.deployEngine();
      const py = await this.findPython();
      const cfg = this.buildConfig(inPath, "", att.key);
      cfg.title = title;
      cfgPath = PathUtils.join(this.workDir, "summary-" + Date.now() + ".json");
      await IOUtils.writeUTF8(cfgPath, JSON.stringify(cfg));

      let summaryEvent = null;
      let errEvent = null;
      const result = await this.runEngine(py, cfgPath, ["--summarize"], job, (ev) => {
        if (ev.type === "progress") {
          prog.setProgress(Math.min(95, Math.round((ev.page / ev.total) * 95)));
          prog.setText(`${title} — ${ev.message}`);
        } else if (ev.type === "log") {
          this.log("engine: " + ev.message);
        } else if (ev.type === "summary") {
          summaryEvent = ev;
        } else if (ev.type === "error") {
          errEvent = ev;
        }
      });

      if (job.cancelled) {
        prog.setError();
        prog.setText(`${title} — 已取消`);
        pw.startCloseTimer(4000);
        return;
      }
      if (errEvent) throw this.err(errEvent.code, errEvent.message, errEvent.trace);
      if (result.exitCode !== 0 || !summaryEvent || !summaryEvent.text) {
        throw this.err("E_SUMMARY", "总结引擎异常退出（exit " + result.exitCode + "）。\n" + result.tail.slice(-8).join("\n"));
      }

      const note = new Zotero.Item("note");
      note.libraryID = parent.libraryID;
      note.parentID = parent.id;
      note.setNote("<h1>论文总结</h1>\n" + this.summaryToHTML(summaryEvent.text));
      await note.saveTx();

      prog.setProgress(100);
      prog.setText(`${title} — 总结已保存到笔记`);
      pw.startCloseTimer(7000);
    } catch (e) {
      this.log("summarizeOne failed: " + (e.stack || e));
      prog.setError();
      prog.setText(`${title} — 失败`);
      pw.startCloseTimer(15000);
      this.alert("生成论文总结失败", this.explain(e), win);
    } finally {
      this._job = null;
      if (cfgPath) {
        try {
          await IOUtils.remove(cfgPath, { ignoreAbsent: true });
        } catch (e) {
          this.log("temporary summary config cleanup failed: " + e);
        }
      }
    }
  },

  summaryToHTML(text) {
    const escape = (value) =>
      value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const lines = text.split(/\r?\n/);
    const html = [];
    let inList = false;
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) {
        if (inList) {
          html.push("</ul>");
          inList = false;
        }
        continue;
      }
      const heading = line.match(/^#{1,3}\s+(.+)$/);
      if (heading) {
        if (inList) {
          html.push("</ul>");
          inList = false;
        }
        html.push("<h2>" + escape(heading[1]) + "</h2>");
      } else if (/^(?:[-*•]|\d+[.)])\s+/.test(line)) {
        if (!inList) {
          html.push("<ul>");
          inList = true;
        }
        html.push("<li>" + escape(line.replace(/^(?:[-*•]|\d+[.)])\s+/, "")) + "</li>");
      } else {
        if (inList) {
          html.push("</ul>");
          inList = false;
        }
        html.push("<p>" + escape(line) + "</p>");
      }
    }
    if (inList) html.push("</ul>");
    return html.join("\n");
  },

  cancel() {
    if (this._job && this._job.proc) {
      this._job.cancelled = true;
      try {
        this._job.proc.kill();
      } catch (e) {
        this.log("kill failed: " + e);
      }
    }
  },

  // ---------------------------------------------------------------- 单篇流程
  async translateOne(att, win) {
    const parent = att.parentItemID ? Zotero.Items.get(att.parentItemID) : null;
    const title = (parent ? parent.getDisplayTitle() : att.getDisplayTitle()) || "未命名";

    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    pw.changeHeadline("全文翻译");
    const prog = new pw.ItemProgress(att.getImageSrc ? att.getImageSrc() : "", title);
    prog.setProgress(0);
    pw.show();

    const job = { cancelled: false, proc: null };
    this._job = job;
    let cfgPath = null;
    try {
      const inPath = await att.getFilePathAsync();
      if (!inPath) {
        throw this.err("E_NOFILE", "附件文件不存在（可能尚未同步下载），请先在 Zotero 中打开一次该 PDF。");
      }
      if (!this.enginePath) await this.deployEngine();
      const py = await this.findPython();

      // 输出路径
      let outDir = (this.getPref("outputDir") || "").trim();
      if (!outDir) outDir = PathUtils.join(Zotero.DataDirectory.dir, "translated-pdfs");
      await IOUtils.makeDirectory(outDir, { createAncestors: true });
      const mode = this.getPref("mode") || "replace";
      const base = Zotero.File.getValidFileName(PathUtils.filename(inPath).replace(/\.pdf$/i, ""));
      const outputSuffix = mode === "bilingual" ? "_zh-bilingual.pdf" : "_zh.pdf";
      const outPath = await this.uniquePath(outDir, base, outputSuffix);
      if (!PathUtils.filename(outPath).toLowerCase().endsWith(outputSuffix)) {
        throw this.err("E_OUTPUT_PATH", "生成的译文文件名未保留预期后缀：" + outPath);
      }
      this.log("translation output path: " + outPath);

      const cfg = this.buildConfig(inPath, outPath, att.key);
      cfgPath = PathUtils.join(this.workDir, "job-" + Date.now() + ".json");
      await IOUtils.writeUTF8(cfgPath, JSON.stringify(cfg));

      let doneEvent = null;
      let errEvent = null;
      const result = await this.runEngine(py, cfgPath, [], job, (ev) => {
        if (ev.type === "progress") {
          const pct = ev.phase === "layout" ? 95 : Math.min(90, Math.round(((ev.page - 1) / ev.total) * 90));
          prog.setProgress(pct);
          prog.setText(`${title} — ${ev.message}`);
        } else if (ev.type === "log") {
          this.log("engine: " + ev.message);
        } else if (ev.type === "done") {
          doneEvent = ev;
        } else if (ev.type === "error") {
          errEvent = ev;
        }
      });

      if (job.cancelled) {
        prog.setError();
        prog.setText(`${title} — 已取消`);
        pw.startCloseTimer(4000);
        return "cancelled";
      }
      if (errEvent) {
        throw this.err(errEvent.code, errEvent.message, errEvent.trace);
      }
      if (result.exitCode !== 0 || !doneEvent) {
        throw this.err(
          "E_ENGINE",
          "翻译引擎异常退出（exit " + result.exitCode + "）。\n" + result.tail.slice(-8).join("\n")
        );
      }
      const normalizePath = (path) => String(path || "").replace(/[\\/]+/g, "\\").toLowerCase();
      if (normalizePath(doneEvent.output) !== normalizePath(outPath) || !(await IOUtils.exists(outPath))) {
        throw this.err(
          "E_OUTPUT_PATH",
          "翻译引擎报告的输出路径与预期不一致，或文件未生成。\n预期：" +
            outPath +
            "\n引擎：" +
            (doneEvent.output || "(无)")
        );
      }

      // 导入 Zotero
      prog.setProgress(98);
      let newAtt = null;
      const attachMode = this.getPref("attachMode") || "import";
      const attTitle = this.TRANSLATED_PREFIX + title;
      const parentItemID = att.parentItemID || undefined;
      if (attachMode === "import") {
        newAtt = await Zotero.Attachments.importFromFile({
          file: outPath,
          fileBaseName: PathUtils.filename(outPath).replace(/\.pdf$/i, ""),
          libraryID: att.libraryID,
          parentItemID,
          title: attTitle,
          contentType: "application/pdf",
        });
        if (newAtt) {
          await this.ensureAttachmentFilename(newAtt, PathUtils.filename(outPath));
        }
      } else if (attachMode === "link") {
        newAtt = await Zotero.Attachments.linkFromFile({
          file: outPath,
          parentItemID,
          title: attTitle,
          contentType: "application/pdf",
        });
      }
      if (newAtt && parent) {
        await this.keepLatestGeneratedAttachment(parent, this.TRANSLATED_PREFIX, newAtt.id);
        await this.keepLatestGeneratedAttachment(parent, this.COMPARISON_PREFIX, null);
      }

      const s = doneEvent.summary || {};
      prog.setProgress(100);
      prog.setText(`${title} — 完成`);
      pw.addDescription(
        `已翻译 ${s.blocks_translated} 段，跳过 ${s.blocks_skipped}，失败 ${s.blocks_failed}；` +
          `缩小排版 ${s.shrunk}，仍溢出 ${s.overflow}；用时 ${s.seconds}s，${s.size_mb} MB`
      );
      pw.addDescription("译文文件：" + outPath);
      if (newAtt && attachMode === "import") {
        pw.addDescription("Zotero 存储文件：" + (await newAtt.getFilePathAsync()));
      }
      if (cfg.debug) pw.addDescription("诊断文件：" + outPath + ".debug.json");
      pw.startCloseTimer(10000);

      if (newAtt && this.getPref("openAfter")) {
        await Zotero.Reader.open(newAtt.id);
      } else if (!newAtt) {
        Zotero.File.reveal(outPath);
      }
      return "ok";
    } catch (e) {
      this.log("translateOne failed: " + (e.stack || e));
      prog.setError();
      prog.setText(`${title} — 失败`);
      pw.startCloseTimer(15000);
      this.alert("全文翻译失败", this.explain(e), win);
      return "error";
    } finally {
      this._job = null;
      if (cfgPath) {
        try {
          await IOUtils.remove(cfgPath, { ignoreAbsent: true });
        } catch (e) {
          /* ignore */
        }
      }
    }
  },

  // ---------------------------------------------------------------- 配置 / 子进程
  buildConfig(inPath, outPath, key) {
    return {
      input: inPath,
      output: outPath,
      cache: PathUtils.join(this.workDir, "cache", key + ".json"),
      ollama_url: (this.getPref("ollamaUrl") || "").trim(),
      model: (this.getPref("model") || "").trim(),
      font: (this.getPref("fontPath") || "").trim(),
      bold_font: (this.getPref("boldFontPath") || "").trim(),
      mode: this.getPref("mode") || "replace",
      bg_fill: this.getPref("bgFill") || "auto",
      min_font_scale: (this.getPref("minFontPercent") || 50) / 100,
      skip_references: !!this.getPref("skipReferences"),
      skip_header_footer: !!this.getPref("skipHeaderFooter"),
      protect_formulas: !!this.getPref("protectFormulas"),
      translate_captions: !!this.getPref("translateCaptions"),
      translate_table_cells: this.getPref("translateTableCells") !== false,
      unify_font: this.getPref("unifyFont") !== false,
      debug: !!this.getPref("debug"),
      page_range: (this.getPref("pageRange") || "").trim(),
      extra_prompt: this.getPref("extraPrompt") || "",
      timeout: this.getPref("timeoutSec") || 300,
      batch_chars: this.getPref("batchChars") || 1800,
    };
  },

  async uniquePath(dir, base, suffix) {
    let p = PathUtils.join(dir, base + suffix);
    for (let i = 2; await IOUtils.exists(p); i++) {
      p = PathUtils.join(dir, `${base}-${i}${suffix}`);
    }
    return p;
  },

  /** 返回 { command, args }。优先用户设置；否则 PATH + 常见安装位置，并逐个试运行，优先选已装 PyMuPDF 的 */
  async findPython() {
    const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    const custom = (this.getPref("pythonPath") || "").trim();
    if (custom) {
      if (!(await IOUtils.exists(custom))) {
        throw this.err("E_PYTHON", "设置里的 Python 路径不存在：" + custom);
      }
      return { command: custom, args: [] };
    }
    if (this._pyCache && (await IOUtils.exists(this._pyCache.command))) return this._pyCache;

    const env = (k) => {
      try {
        return Services.env.get(k) || "";
      } catch (e) {
        return "";
      }
    };
    const cands = [];
    const add = (command, args = []) => {
      if (command && !/WindowsApps/i.test(command) && !cands.some((c) => c.command === command && c.args.join() === args.join())) {
        cands.push({ command, args });
      }
    };
    const exists = async (p) => {
      try {
        return await IOUtils.exists(p);
      } catch (e) {
        return false;
      }
    };
    const children = async (dir) => {
      try {
        return (await IOUtils.getChildren(dir)).sort().reverse();
      } catch (e) {
        return [];
      }
    };

    // 1) PATH
    for (const n of Zotero.isWin ? ["python.exe", "python3.exe", "python", "python3"] : ["python3", "python"]) {
      try {
        add(await Subprocess.pathSearch(n));
      } catch (e) {
        /* 没有就算了 */
      }
    }
    // 2) 常见安装位置（Zotero 作为图形程序启动时 PATH 往往不完整）
    if (Zotero.isWin) {
      const local = env("LOCALAPPDATA");
      const roots = [
        local && PathUtils.join(local, "Programs", "Python"),
        env("ProgramFiles"),
        env("ProgramFiles(x86)"),
        "C:\\",
      ].filter(Boolean);
      for (const root of roots) {
        for (const d of await children(root)) {
          if (/[\\/]Python3\d*/i.test(d)) {
            const p = PathUtils.join(d, "python.exe");
            if (await exists(p)) add(p);
          }
        }
      }
      const home = env("USERPROFILE");
      const pdata = env("ProgramData");
      for (const base of [home, pdata].filter(Boolean)) {
        for (const name of ["anaconda3", "miniconda3", "Anaconda3", "Miniconda3", "mambaforge", "miniforge3"]) {
          const p = PathUtils.join(base, name, "python.exe");
          if (await exists(p)) add(p);
        }
      }
      const winDir = env("WINDIR") || "C:\\Windows";
      const py = PathUtils.join(winDir, "py.exe");
      if (await exists(py)) add(py, ["-3"]);
    } else {
      for (const p of [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        "/opt/miniconda3/bin/python3",
        "/opt/anaconda3/bin/python3",
      ]) {
        if (await exists(p)) add(p);
      }
    }

    // 3) 逐个试运行：exit 0 = 可用且已装 PyMuPDF；3 = 可用但缺 PyMuPDF；其他 = 不可用（如 Windows 商店占位程序）
    const code = "import sys\ntry:\n import pymupdf\nexcept Exception:\n try:\n  import fitz\n except Exception:\n  sys.exit(3)\n";
    let usable = null;
    const tried = [];
    for (const c of cands) {
      try {
        const proc = await Subprocess.call({
          command: c.command,
          arguments: [...c.args, "-c", code],
          stderr: "stdout",
        });
        while (await proc.stdout.readString()) {
          /* 丢弃输出 */
        }
        const { exitCode } = await proc.wait();
        tried.push(`${c.command} → exit ${exitCode}`);
        if (exitCode === 0) {
          this._pyCache = c;
          return c;
        }
        if (exitCode === 3 && !usable) usable = c;
      } catch (e) {
        tried.push(`${c.command} → ${e}`);
      }
    }
    if (usable) return usable; // 引擎会给出"缺少 PyMuPDF"的明确提示
    throw this.err(
      "E_PYTHON",
      "没有找到可用的 Python。" +
        (tried.length ? "\n已尝试：\n" + tried.join("\n") : "") +
        "\n\n请安装 Python 3.9+（安装时勾选 Add python.exe to PATH），或在设置里手动填写 Python 路径。\n" +
        "Windows 可在命令提示符执行 where python 查看路径。"
    );
  },

  async runEngine(pyInfo, cfgPath, extraArgs, job, onEvent) {
    const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    let proc;
    try {
      proc = await Subprocess.call({
        command: pyInfo.command,
        arguments: [...pyInfo.args, this.enginePath, "--config", cfgPath, ...extraArgs],
        environmentAppend: true,
        environment: { PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
        stderr: "stdout",
      });
    } catch (e) {
      throw this.err("E_PYTHON", "无法启动 Python（" + pyInfo.command + "）：" + e);
    }
    if (job) job.proc = proc;

    const tail = [];
    const handle = (line) => {
      line = line.trim();
      if (!line) return;
      try {
        const ev = JSON.parse(line);
        if (ev && ev.type) {
          onEvent(ev);
          return;
        }
      } catch (e) {
        /* 非 JSON 行：多半是 Python traceback */
      }
      tail.push(line);
      if (tail.length > 40) tail.shift();
    };

    let buf = "";
    let chunk;
    while ((chunk = await proc.stdout.readString())) {
      buf += chunk;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        handle(buf.slice(0, i));
        buf = buf.slice(i + 1);
      }
    }
    handle(buf);
    const { exitCode } = await proc.wait();
    return { exitCode, tail };
  },

  /** 设置面板的"环境检测"按钮 */
  async checkEnvironment() {
    if (!this.enginePath) await this.deployEngine();
    const py = await this.findPython();
    const cfgPath = PathUtils.join(this.workDir, "check-" + Date.now() + ".json");
    await IOUtils.writeUTF8(cfgPath, JSON.stringify(this.buildConfig("", "", "check")));
    const lines = ["Python 路径：" + py.command + (py.args.length ? " " + py.args.join(" ") : "")];
    let checkEv = null;
    let errEv = null;
    try {
      const r = await this.runEngine(py, cfgPath, ["--check"], null, (ev) => {
        if (ev.type === "check") checkEv = ev;
        if (ev.type === "error") errEv = ev;
      });
      if (checkEv) {
        for (const it of checkEv.items) lines.push(`${it.ok ? "✓" : "✗"} ${it.name}：${it.detail}`);
        lines.push(checkEv.ok ? "\n全部就绪，可以开始翻译。" : "\n存在未通过项，请按提示处理后重试。");
      } else if (errEv) {
        lines.push("✗ " + errEv.message);
      } else {
        lines.push("✗ 引擎无输出（exit " + r.exitCode + "）：\n" + r.tail.slice(-6).join("\n"));
      }
    } finally {
      await IOUtils.remove(cfgPath, { ignoreAbsent: true });
    }
    return { lines };
  },

  // ---------------------------------------------------------------- 错误
  err(code, message, detail) {
    const e = new Error(message);
    e.code = code;
    e.detail = detail;
    return e;
  },

  explain(e) {
    const hints = {
      E_PYTHON: "\n\n提示：先在设置面板点「检测」，按提示安装依赖：pip install pymupdf fonttools",
      E_NO_PYMUPDF: "\n\n提示：在终端执行  pip install pymupdf fonttools  （务必用设置里同一个 Python）",
      E_OLLAMA_DOWN: "\n\n提示：运行 ollama serve（桌面版需保持 Ollama 程序已启动），并检查设置里的服务地址。",
      E_MODEL_MISSING: "\n\n提示：在终端执行  ollama pull <模型名>  后重试。",
      E_FONT: "\n\n提示：在设置里选择一个包含中文的 .ttf / .otf 字体文件。",
      E_NO_TEXT: "\n\n提示：扫描版 PDF 需要先 OCR（如 ocrmypdf）生成文字层后再翻译。",
    };
    let msg = (e && e.message) || String(e);
    if (e && e.code) msg = `[${e.code}] ${msg}` + (hints[e.code] || "");
    return msg;
  },
};
