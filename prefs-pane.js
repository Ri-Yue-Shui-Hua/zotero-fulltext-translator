/* 设置面板脚本：浏览按钮 + 环境检测
 * 用事件委托绑定（data-ftt-action），不依赖内联 oncommand，也不依赖脚本与 DOM 的加载先后。 */
var FTTPrefs = {
  out(msg) {
    const el = document.getElementById("ftt-test-output");
    if (el) el.textContent = msg;
  },

  /** 返回所选路径；取消返回 null */
  async choose(kind) {
    const title =
      kind === "folder" ? "选择 PDF 输出目录" : kind === "font" ? "选择中文字体文件" : "选择 Python 可执行文件";
    try {
      const { FilePicker } = ChromeUtils.importESModule("chrome://zotero/content/modules/filePicker.mjs");
      const fp = new FilePicker();
      fp.init(window, title, kind === "folder" ? fp.modeGetFolder : fp.modeOpen);
      if (kind === "font") fp.appendFilter("字体文件 (ttf/otf/ttc)", "*.ttf;*.otf;*.ttc");
      fp.appendFilters(fp.filterAll);
      const rv = await fp.show();
      return rv === fp.returnOK || rv === fp.returnReplace ? fp.file : null;
    } catch (e1) {
      // 备用：Firefox 原生 nsIFilePicker
      const fp = Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker);
      fp.init(window.browsingContext, title, kind === "folder" ? fp.modeGetFolder : fp.modeOpen);
      if (kind === "font") fp.appendFilter("字体文件 (ttf/otf/ttc)", "*.ttf;*.otf;*.ttc");
      fp.appendFilters(fp.filterAll);
      return await new Promise((resolve) =>
        fp.open((rv) => resolve(rv === fp.returnOK || rv === fp.returnReplace ? fp.file.path : null))
      );
    }
  },

  async pick(inputId, kind) {
    try {
      const path = await this.choose(kind);
      if (!path) return;
      const input = document.getElementById(inputId);
      input.value = path;
      // 让 Zotero 的 preference 绑定感知到变化
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (e) {
      Services.prompt.alert(window, "全文翻译", "打开文件选择器失败：" + (e && e.message ? e.message : e));
    }
  },

  async test() {
    this.out("检测中…");
    try {
      if (!Zotero.FTT) throw new Error("插件核心未加载（启动时出错），请查看 Browser Console / 调试日志中的 [FTT] 信息");
      const res = await Zotero.FTT.checkEnvironment();
      this.out(res.lines.join("\n"));
    } catch (e) {
      this.out("✗ " + (e && e.message ? e.message : e));
    }
  },

  bind() {
    if (window.__fttBound) return;
    window.__fttBound = true;
    document.addEventListener("command", (ev) => {
      const el = ev.target && ev.target.closest ? ev.target.closest("[data-ftt-action]") : null;
      if (!el) return;
      const action = el.getAttribute("data-ftt-action");
      if (action === "pick") this.pick(el.getAttribute("data-ftt-input"), el.getAttribute("data-ftt-kind"));
      else if (action === "test") this.test();
    });
  },
};
FTTPrefs.bind();
